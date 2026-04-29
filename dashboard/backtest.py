"""
backtest.py — Walk-forward backtest of the SMC + POI entry strategy on IDX stocks.

Uses only Yahoo Finance daily OHLCV (free, unlimited).
GoAPI broker-flow data is not available historically, so this backtest validates
the technical leg of the strategy (SMC bias + Near POI + optional Trend/Volume).

Entry rules:
  - Rolling 60-day window, stepped every 3 days to prevent signal clustering
  - Entry condition: 4H-proxy SMC bias == Bullish AND price near POI (±3%)
  - Entry price: next bar's Open
  - SL: poi_low × 0.985  (1.5% below demand zone floor)
  - TP: entry + (entry − SL) × 2.5  (2.5 R)
  - Max hold: 20 calendar days, then exit at close

Results are cached to disk for 24 hours.
"""

import json
import time
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

# ── Strategy parameters ───────────────────────────────────────────────────────
TP_R       = 2.5    # take-profit as a multiple of risk
SL_FACTOR  = 0.015  # SL distance = poi_low × SL_FACTOR below poi_low
MAX_HOLD   = 20     # max bars to hold before forced exit
WINDOW     = 60     # bars in each SMC detection window
STEP       = 3      # bars to advance between signal checks (avoids duplication)
POI_BAND   = 0.03   # price must be within ±3% of POI zone


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


def run_backtest(tickers: list | None = None, force: bool = False) -> dict:
    """
    Walk-forward backtest.  Returns a dict with:
      trades       — list of individual trade records
      equity_curve — cumulative R after each trade
      dd_curve     — drawdown from equity peak after each trade
      metrics      — summary statistics
      ticker_stats — per-ticker breakdown
      params       — strategy parameters used
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

    # ── Batch download 2 years of daily data ───────────────────────────────────
    try:
        raw = yf.download(
            tickers_str, period="2y", interval="1d",
            group_by="ticker", progress=False, auto_adjust=True,
        )
    except Exception as e:
        return {"error": f"Data download failed: {e}"}

    all_trades: list[dict] = []
    skipped:    list[str]  = []

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

                # ── Trend check (MA20) ────────────────────────────────────────
                close_win = closes[end - WINDOW: end]
                ma20 = float(pd.Series(close_win).rolling(20).mean().iloc[-1])
                trend_valid = bool(curr_close > ma20)

                # ── Volume check (1.5× 10-day avg) ────────────────────────────
                vol_win = vols[end - WINDOW: end]
                avg10v  = float(pd.Series(vol_win).rolling(10).mean().iloc[-1])
                vol_valid = avg10v > 0 and bool(vols[end - 1] > 1.5 * avg10v)

                # ── Entry at next bar open ────────────────────────────────────
                entry_idx   = end
                entry_price = float(opens[entry_idx])
                if entry_price <= 0:
                    continue

                sl = poi_low  * (1 - SL_FACTOR)
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
                if trend_valid: flags.append("Uptrend")
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
    monthly: dict[str, float] = {}
    for tr in all_trades:
        month = tr["entry_date"][:7]  # "YYYY-MM"
        monthly[month] = round(monthly.get(month, 0.0) + tr["pnl_r"], 3)

    # ── Per-ticker breakdown ──────────────────────────────────────────────────
    ticker_stats: dict[str, dict] = {}
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
        "total_trades":       total,
        "wins":               len(wins),
        "losses":             len(losses),
        "win_rate":           round(win_rate, 1),
        "total_r":            round(total_r, 2),
        "avg_win_r":          round(avg_win_r,  3),
        "avg_loss_r":         round(avg_loss_r, 3),
        "profit_factor":      round(profit_factor, 2),
        "max_drawdown_r":     round(max_dd, 2),
        "expectancy_r":       round(expectancy, 3),
        "avg_hold_days":      round(avg_hold, 1),
        "avg_pnl_pct":        round(avg_pnl_pct, 2),
        "max_consec_wins":    max_consec_wins,
        "max_consec_losses":  max_consec_losses,
        "tickers_tested":     len(tickers),
        "tickers_with_trades": len(ticker_stats),
        "tickers_skipped":    len(skipped),
        "best_trade":         best_trade,
        "worst_trade":        worst_trade,
    }

    result = {
        "trades":       all_trades,
        "equity_curve": eq_curve,
        "dd_curve":     dd_curve,
        "metrics":      metrics,
        "ticker_stats": ticker_stats,
        "monthly_pnl":  monthly,
        "params": {
            "tp_r":      TP_R,
            "sl_factor": SL_FACTOR,
            "max_hold":  MAX_HOLD,
            "window":    WINDOW,
            "step":      STEP,
            "poi_band":  POI_BAND,
            "universe":  len(tickers),
        },
    }

    _save_bt_cache(result)
    _cache_set("backtest_result", result)
    return result
