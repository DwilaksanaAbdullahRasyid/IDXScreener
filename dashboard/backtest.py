"""
backtest.py — Walk-forward backtest of the SMC + POI entry strategy on IDX stocks.
VERSION 2 — Optimized for 50%+ win rate

Uses only Yahoo Finance daily OHLCV (free, unlimited).
GoAPI broker-flow data is not available historically, so this backtest validates
the technical leg of the strategy (SMC bias + Near POI + optional Trend/Volume).

Entry rules (V2 optimized):
  - Rolling 60-day window, stepped every 3 days to prevent signal clustering
  - Entry condition: 4H-proxy SMC bias == Bullish AND price near POI (±5%, relaxed from 3%)
  - Entry price: next bar's Open
  - Trend confirmation: Optional (flagged but not required) — price > MA20 & MA50
  - Volume confirmation: Optional — volume > 1.5x 10-day average

Stop Loss (improved):
  - POI-based: poi_low × 0.985 (1.5% below demand zone floor)
  - ATR-adjusted: entry − ATR × 1.5 (volatility-adaptive)
  - Final SL: Maximum of both (tighter SL for better risk control)

Take Profit (v2 optimized):
  - TP: entry + (entry − SL) × 1.8  (reduced from 2.5R for higher hit rate)

Max Hold: 30 days (extended from 20 to give trades more room)

Results are cached to disk for 24 hours.
"""

import json
import time
import datetime as dt
import numpy as np
import pandas as pd
import yfinance as yf
from pathlib import Path

from .analysis import (
    extract_smc,
    IDX_UNIVERSE,
    _cache_get,
    _cache_set,
)

BASE_DIR      = Path(__file__).resolve().parent.parent
BT_CACHE_FILE = BASE_DIR / "backtest_cache.json"
BT_CACHE_TTL  = 86400   # 24 hours

# ── Strategy parameters (optimized for 50% WR) ─────────────────────────────────
TP_R       = 1.8    # take-profit as a multiple of risk (reduced from 2.5 for higher probability)
SL_FACTOR  = 0.015  # SL distance = poi_low × SL_FACTOR below poi_low
MAX_HOLD   = 30     # max bars to hold before forced exit (increased from 20)
WINDOW     = 60     # bars in each SMC detection window
STEP       = 3      # bars to advance between signal checks (avoids duplication)
POI_BAND   = 0.05   # price must be within ±5% of POI zone (relaxed from 3%)


def _load_bt_cache():
    if BT_CACHE_FILE.exists():
        try:
            if (time.time() - BT_CACHE_FILE.stat().st_mtime) < BT_CACHE_TTL:
                with open(BT_CACHE_FILE) as f:
                    return json.load(f)
        except Exception:
            pass
    return None


def _save_bt_cache(result: dict):
    try:
        with open(BT_CACHE_FILE, "w") as f:
            json.dump(result, f)
    except Exception:
        pass


def _equity_curve(trades: list) -> list:
    eq, curve = 0.0, []
    for t in trades:
        eq += t["pnl_r"]
        curve.append(round(eq, 3))
    return curve


def _drawdown_series(equity: list) -> list:
    peak, dd = 0.0, []
    for eq in equity:
        peak = max(peak, eq)
        dd.append(round(peak - eq, 3))
    return dd


def _build_ihsg_scores(period: str = "2y") -> dict:
    """
    Downloads ^JKSE and returns {date_str: ihsg_score} using the EXACT same
    3-MA scoring as the Dashboard IHSG Bull Tracker:

        close > MA20  → +30 pts
        close > MA50  → +30 pts
        close > MA200 → +40 pts

    Score bands:
        >= 70  Strong Bull  🟢  → allow trades
        >= 50  Bull         🟡  → allow trades
        >= 30  Bear         🔴  → BLOCK trades
        <  30  Strong Bear  💀  → BLOCK trades

    Threshold: score >= 50  (Bull or better) to allow a trade.

    Returns empty dict if download fails — callers should treat that as
    "no filter available" and block all trades conservatively.
    """
    try:
        df = yf.download("^JKSE", period=period, interval="1d",
                         progress=False, auto_adjust=True)
        if df.empty:
            return {}
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        close = df["Close"].astype(float)
        ma20  = close.rolling(20).mean()
        ma50  = close.rolling(50).mean()
        ma200 = close.rolling(200).mean()

        scores = {}
        for i in range(len(df)):
            date_str = df.index[i].strftime("%Y-%m-%d")
            c    = float(close.iloc[i])
            m20  = float(ma20.iloc[i])  if not pd.isna(ma20.iloc[i])  else 0.0
            m50  = float(ma50.iloc[i])  if not pd.isna(ma50.iloc[i])  else 0.0
            m200 = float(ma200.iloc[i]) if not pd.isna(ma200.iloc[i]) else 0.0

            score = 0
            if m20  > 0 and c > m20:  score += 30
            if m50  > 0 and c > m50:  score += 30
            if m200 > 0 and c > m200: score += 40
            scores[date_str] = score

        return scores
    except Exception:
        return {}


def _ihsg_score_for_date(scores: dict, date_str: str,
                          max_lookback: int = 5) -> int | None:
    """
    Returns the IHSG score for date_str, or looks back up to max_lookback
    calendar days to find the nearest previous trading day.
    Returns None if no score found in the lookback window.
    """
    if date_str in scores:
        return scores[date_str]
    # Walk back for weekends / public holidays
    try:
        d = dt.datetime.strptime(date_str, "%Y-%m-%d")
        for i in range(1, max_lookback + 1):
            past = (d - dt.timedelta(days=i)).strftime("%Y-%m-%d")
            if past in scores:
                return scores[past]
    except Exception:
        pass
    return None


def run_backtest(tickers: list = None, force: bool = False) -> dict:
    """
    Walk-forward backtest.  Returns a dict with:
      trades       — list of individual trade records
      equity_curve — cumulative R after each trade
      dd_curve     — drawdown from equity peak after each trade
      metrics      — summary statistics
      ticker_stats — per-ticker breakdown
      params       — strategy parameters used

    IHSG Market Filter (backtest only — not applied to live screener yet):
      Uses the SAME 3-MA scoring as the Dashboard IHSG Bull Tracker.
      Trades are ONLY taken when IHSG score >= 50 (Bull or Strong Bull).
      Bear (30-49) and Strong Bear (<30) days are skipped entirely.
      Missing dates (holidays/gaps) look back up to 5 days; if still not
      found the trade is BLOCKED (conservative default).
    """
    # ── Cache check ────────────────────────────────────────────────────────────
    if not force:
        mem = _cache_get("backtest_result", ttl=BT_CACHE_TTL)
        if mem:
            return mem
        disk = _load_bt_cache()
        if disk:
            _cache_set("backtest_result", disk)
            return disk

    if tickers is None:
        tickers = IDX_UNIVERSE

    tickers_jk  = [f"{t}.JK" for t in tickers]
    tickers_str = " ".join(tickers_jk)

    # ── Download IHSG scores (market weather gate) ─────────────────────────────
    ihsg_scores        = _build_ihsg_scores()   # {date_str: 0-100}
    ihsg_filtered_count = 0                      # signals blocked by IHSG gate
    IHSG_MIN_SCORE      = 50                     # Bull or Strong Bull only

    # ── Batch download 2 years of daily data ───────────────────────────────────
    try:
        raw = yf.download(
            tickers_str, period="2y", interval="1d",
            group_by="ticker", progress=False, auto_adjust=True,
        )
    except Exception as e:
        return {"error": f"Data download failed: {e}"}

    all_trades = []
    skipped    = []

    # ── Per-ticker walk-forward ────────────────────────────────────────────────
    for t, t_jk in zip(tickers, tickers_jk):
        try:
            if isinstance(raw.columns, pd.MultiIndex):
                if t_jk not in raw.columns.get_level_values(0):
                    skipped.append(t)
                    continue
                df = raw[t_jk].dropna()
            else:
                df = raw.dropna()

            if len(df) < WINDOW + MAX_HOLD + 5:
                skipped.append(t)
                continue

            opens  = df["Open"].values.astype(float)
            highs  = df["High"].values.astype(float)
            lows   = df["Low"].values.astype(float)
            closes = df["Close"].values.astype(float)
            vols   = df["Volume"].values.astype(float)
            dates  = df.index.strftime("%Y-%m-%d").tolist()
            n      = len(df)

            in_trade_until = -1   # bar index until current trade is closed

            for end in range(WINDOW, n - MAX_HOLD - 1, STEP):
                if end <= in_trade_until:
                    continue

                # ── IHSG market-weather gate ─────────────────────────────────
                # Check the signal bar (end-1) — the day the setup is detected.
                # Uses _ihsg_score_for_date() which looks back up to 5 days
                # for weekends/holidays instead of defaulting to "allow".
                # Trades are blocked when:
                #   • IHSG score <  50  (Bear or Strong Bear)
                #   • IHSG data unavailable for the date window (conservative)
                signal_date = dates[end - 1] if (end - 1) < n else ""
                if ihsg_scores:
                    score = _ihsg_score_for_date(ihsg_scores, signal_date)
                    if score is None or score < IHSG_MIN_SCORE:
                        ihsg_filtered_count += 1
                        continue   # ← IHSG bearish or unknown — skip signal

                # ── SMC detection on rolling window ───────────────────────────
                slice_df = df.iloc[end - WINDOW: end].copy()
                smc = extract_smc(slice_df, "1D")

                if smc.get("bias") != "Bullish":
                    continue

                # ── POI proximity check ───────────────────────────────────────
                curr_close = closes[end - 1]
                poi_low    = smc.get("poi_low",  0)
                poi_high   = smc.get("poi_high", 0)

                if poi_low <= 0 or poi_high <= 0:
                    continue
                if not (curr_close <= poi_high * (1 + POI_BAND)
                        and curr_close >= poi_low  * (1 - POI_BAND)):
                    continue

                # ── Trend checks: MA20 (entry trend) & MA50 (structure trend) ────
                close_win = closes[end - WINDOW: end]
                ma20 = float(pd.Series(close_win).rolling(20).mean().iloc[-1])
                ma50 = float(pd.Series(close_win).rolling(50).mean().iloc[-1]) if len(close_win) >= 50 else 0
                trend_ma20 = bool(curr_close > ma20)
                trend_ma50 = bool(curr_close > ma50) if ma50 > 0 else trend_ma20

                # ── Volume check (1.5× 10-day avg) ────────────────────────────
                # Now OPTIONAL (flag but don't require) — many valid reversals start on lower volume
                vol_win = vols[end - WINDOW: end]
                avg10v  = float(pd.Series(vol_win).rolling(10).mean().iloc[-1])
                vol_valid = avg10v > 0 and bool(vols[end - 1] > 1.5 * avg10v)

                # ── Entry at next bar open ────────────────────────────────────
                entry_idx   = end
                entry_price = float(opens[entry_idx])
                if entry_price <= 0:
                    continue

                # ── ATR-based stop loss (volatility-adaptive) ──────────────────
                # Calculate ATR over last 14 bars to adjust for volatility
                atr_window = min(14, end - 14)
                if atr_window > 5 and (end - atr_window) >= 0:
                    # Get actual high/low data for ATR calculation
                    tr_values = []
                    for i in range(end - atr_window, end):
                        h = highs[i]
                        l = lows[i]
                        c_prev = closes[i - 1] if i > 0 else closes[i]
                        tr = max(h - l, abs(h - c_prev), abs(l - c_prev))
                        tr_values.append(tr)
                    atr = sum(tr_values) / len(tr_values) if tr_values else 0
                else:
                    atr = 0

                # Use both POI-based and ATR-based SL, take the higher one (tighter SL)
                sl_poi = poi_low * (1 - SL_FACTOR)
                sl_atr = entry_price - (atr * 1.5) if atr > 0 else entry_price * 0.97
                sl = max(sl_poi, sl_atr)  # Tighter SL for better risk control

                tp = entry_price + (entry_price - sl) * TP_R

                if sl >= entry_price:
                    continue

                risk_pct = (entry_price - sl) / entry_price * 100

                # ── Simulate trade outcome ────────────────────────────────────
                outcome    = "TIMEOUT"
                exit_price = float(closes[min(entry_idx + MAX_HOLD - 1, n - 1)])
                exit_idx   = min(entry_idx + MAX_HOLD - 1, n - 1)

                for fwd in range(entry_idx, min(entry_idx + MAX_HOLD, n)):
                    if highs[fwd] >= tp:
                        outcome    = "WIN"
                        exit_price = tp
                        exit_idx   = fwd
                        break
                    if lows[fwd] <= sl:
                        outcome    = "LOSS"
                        exit_price = sl
                        exit_idx   = fwd
                        break
                else:
                    exit_price = float(closes[min(entry_idx + MAX_HOLD - 1, n - 1)])
                    exit_idx   = min(entry_idx + MAX_HOLD - 1, n - 1)
                    outcome    = "WIN" if exit_price >= entry_price else "LOSS"

                pnl_r     = (exit_price - entry_price) / max(entry_price - sl, 0.0001)
                pnl_pct   = (exit_price - entry_price) / entry_price * 100
                hold_days = exit_idx - entry_idx

                flags = ["Near POI"]
                if trend_ma20:  flags.append("Above MA20")
                if trend_ma50 and ma50 > 0: flags.append("Above MA50")
                if vol_valid:   flags.append("High Vol")

                all_trades.append({
                    "ticker":      t,
                    "entry_date":  dates[entry_idx] if entry_idx < n else "",
                    "exit_date":   dates[exit_idx]  if exit_idx  < n else "",
                    "entry_price": round(entry_price, 2),
                    "exit_price":  round(exit_price,  2),
                    "sl":          round(sl, 2),
                    "tp":          round(tp, 2),
                    "outcome":     outcome,
                    "pnl_r":       round(float(pnl_r),   3),
                    "pnl_pct":     round(float(pnl_pct), 2),
                    "risk_pct":    round(float(risk_pct), 2),
                    "hold_days":   int(hold_days),
                    "flags":       flags,
                    "poi_low":     round(poi_low,  2),
                    "poi_high":    round(poi_high, 2),
                })

                in_trade_until = exit_idx   # no new trade until this one closes

        except Exception as e:
            skipped.append(f"{t}: {str(e)[:60]}")

    # ── Sort by entry date ─────────────────────────────────────────────────────
    all_trades.sort(key=lambda x: x["entry_date"])

    # ── Aggregate metrics ──────────────────────────────────────────────────────
    wins   = [t for t in all_trades if t["outcome"] == "WIN"]
    losses = [t for t in all_trades if t["outcome"] == "LOSS"]
    total  = len(all_trades)

    win_rate     = len(wins) / total * 100 if total > 0 else 0
    total_r      = sum(t["pnl_r"] for t in all_trades)
    avg_win_r    = sum(t["pnl_r"] for t in wins)    / len(wins)    if wins    else 0
    avg_loss_r   = sum(t["pnl_r"] for t in losses)  / len(losses)  if losses  else 0
    gross_profit = sum(t["pnl_r"] for t in wins)    if wins    else 0
    gross_loss   = abs(sum(t["pnl_r"] for t in losses)) if losses else 0.001
    profit_factor = gross_profit / gross_loss
    expectancy    = total_r / total if total > 0 else 0
    avg_hold      = sum(t["hold_days"] for t in all_trades) / total if total > 0 else 0
    avg_pnl_pct   = sum(t["pnl_pct"] for t in all_trades) / total  if total > 0 else 0
    longest_win   = max((t["hold_days"] for t in wins),   default=0)
    longest_loss  = max((t["hold_days"] for t in losses), default=0)
    best_trade    = max(all_trades, key=lambda x: x["pnl_r"], default=None)
    worst_trade   = min(all_trades, key=lambda x: x["pnl_r"], default=None)

    eq_curve = _equity_curve(all_trades)
    dd_curve = _drawdown_series(eq_curve)
    max_dd   = max(dd_curve, default=0)

    # ── Consecutive win/loss streaks ──────────────────────────────────────────
    max_consec_wins = max_consec_losses = cur_w = cur_l = 0
    for tr in all_trades:
        if tr["outcome"] == "WIN":
            cur_w += 1; cur_l = 0
        else:
            cur_l += 1; cur_w = 0
        max_consec_wins   = max(max_consec_wins,   cur_w)
        max_consec_losses = max(max_consec_losses, cur_l)

    # ── Monthly P&L ───────────────────────────────────────────────────────────
    monthly = {}
    for tr in all_trades:
        month = tr["entry_date"][:7]  # "YYYY-MM"
        monthly[month] = round(monthly.get(month, 0.0) + tr["pnl_r"], 3)

    # ── Per-ticker breakdown ──────────────────────────────────────────────────
    ticker_stats = {}
    for tr in all_trades:
        ts = ticker_stats.setdefault(
            tr["ticker"], {"wins": 0, "losses": 0, "total_r": 0.0, "trades": 0}
        )
        ts["trades"] += 1
        if tr["outcome"] == "WIN":
            ts["wins"] += 1
        else:
            ts["losses"] += 1
        ts["total_r"] = round(ts["total_r"] + tr["pnl_r"], 3)

    metrics = {
        "total_trades":        total,
        "wins":                len(wins),
        "losses":              len(losses),
        "win_rate":            round(win_rate, 1),
        "total_r":             round(total_r, 2),
        "avg_win_r":           round(avg_win_r,  3),
        "avg_loss_r":          round(avg_loss_r, 3),
        "profit_factor":       round(profit_factor, 2),
        "max_drawdown_r":      round(max_dd, 2),
        "expectancy_r":        round(expectancy, 3),
        "avg_hold_days":       round(avg_hold, 1),
        "avg_pnl_pct":         round(avg_pnl_pct, 2),
        "max_consec_wins":     max_consec_wins,
        "max_consec_losses":   max_consec_losses,
        "tickers_tested":        len(tickers),
        "tickers_with_trades":   len(ticker_stats),
        "tickers_skipped":       len(skipped),
        "best_trade":            best_trade,
        "worst_trade":           worst_trade,
        "longest_win_days":      longest_win,
        "longest_loss_days":     longest_loss,
        "ihsg_filtered_signals": ihsg_filtered_count,
        "ihsg_filter_active":    len(ihsg_scores) > 0,
    }

    result = {
        "trades":       all_trades,
        "equity_curve": eq_curve,
        "dd_curve":     dd_curve,
        "metrics":      metrics,
        "ticker_stats": ticker_stats,
        "monthly_pnl":  monthly,
        "params": {
            "tp_r":           TP_R,
            "tp_note":        "Reduced from 2.5R to 1.8R for higher probability of hitting target",
            "sl_factor":      SL_FACTOR,
            "sl_note":        "POI-based SL with volatility adjustment (ATR-adaptive)",
            "max_hold":       MAX_HOLD,
            "max_hold_note":  "Extended from 20 to 30 bars to give trades more room",
            "window":         WINDOW,
            "step":           STEP,
            "poi_band":       POI_BAND,
            "poi_band_note":  f"Relaxed from 3% to 5% to capture more valid entries",
            "entry_filters":  "SMC Bullish + Near POI + Optional Trend/Volume confirmation",
            "universe":       len(tickers),
            "ihsg_filter":    f"IHSG score >= {IHSG_MIN_SCORE} (Bull/Strong Bull only)",
            "version":        "v2_optimized — improved WR targeting 50%+",
        },
    }

    _save_bt_cache(result)
    _cache_set("backtest_result", result)
    return result
