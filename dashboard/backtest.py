"""
backtest.py — Walk-forward backtest of the SMC + POI entry strategy on IDX stocks.
VERSION 3.2 — 30-day daily HTF · 10-year period · Grade A signals only

V3.2 changes vs V3.1:
  • WEEKLY_WINDOW removed — replaced by DAILY_HTF_WINDOW = 30
    (30-day rolling daily window for bull/bear confirmation; no extra download)
  • Weekly data download removed — HTF bias computed from same daily data
  • BT_PERIOD 4y → 10y (daily data available; minimum 100 trades target)
  • _is_rejection_candle() moved to analysis.py (shared with live screener)
  • Filter 5 label: "weekly" → "htf_daily"

Entry filters (all 6 must pass):
  1. IHSG market-weather gate (score >= 50, Bull or Strong Bull)
  2. Daily SMC bias = Bullish (BOS or CHoCH via LuxAlgo algorithm)
  3. Price within POI demand zone (±5%)
  4. Rejection candle on signal bar (green candle or relaxed hammer)
  5. 30-day daily HTF bias = Bullish (rolling 30-bar daily SMC window)
  6. Close above MA20 (short-term uptrend) + Volume ≥ 1.1× 10-day average

Stop Loss: tighter of POI-based (poi_low × 0.985) or ATR × 1.5

Take Profit (partial exits — 1/3 position at each level):
  TP1 = entry + 1R  → exit 1/3, move SL to breakeven
  TP2 = entry + 2R  → exit 1/3, move SL to TP1
  TP3 = entry + 3R  → exit final 1/3

P&L outcomes:
  Full loss (no TP hit):     −1.0R
  TP1 only → stopped at BE:  +0.33R
  TP1+TP2 → stopped at TP1:  +1.33R
  All 3 TPs hit:             +2.0R

Backtest period: 10 years daily data
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
    _is_rejection_candle,   # shared helper — moved to analysis.py in V3.2
)

BASE_DIR      = Path(__file__).resolve().parent.parent
BT_CACHE_FILE = BASE_DIR / "backtest_cache.json"
BT_CACHE_TTL  = 86400   # 24 hours

# ── Strategy parameters (V3.2) ────────────────────────────────────────────────
TP1_R            = 1.0    # first partial exit (1R)
TP2_R            = 2.0    # second partial exit (2R)
TP3_R            = 3.0    # final exit (3R)
SL_FACTOR        = 0.015  # SL = poi_low × (1 - SL_FACTOR) below demand zone
MAX_HOLD         = 40     # max bars to hold (allows TP3 to develop)
WINDOW           = 60     # rolling SMC detection window (daily bars)
STEP             = 3      # advance step between signal checks
POI_BAND         = 0.05   # price must be within ±5% of POI zone
DAILY_HTF_WINDOW = 30     # 30-day daily rolling window for HTF bull/bear confirmation
                           # (replaces WEEKLY_WINDOW=26; no extra download needed)
BT_PERIOD        = "10y"  # 10-year backtest period (daily "1d"; yfinance supports "10y")
VOL_MULT         = 1.1    # volume must be ≥ this × 10-day average (slightly above avg)


# ── Helper functions ──────────────────────────────────────────────────────────
# NOTE: _is_rejection_candle() moved to analysis.py (V3.2) so it is shared
# between the live screener (1H bars) and the backtest (daily bars).
# It is imported at the top of this file.


def _build_daily_htf_map(df_daily: pd.DataFrame) -> dict:
    """
    Rolls a DAILY_HTF_WINDOW-bar window over daily OHLCV and returns
    {date_str: "Bullish"|"Bearish"|"Neutral"} for HTF confirmation.

    Uses the same daily data already downloaded — zero extra downloads.
    Replaces the weekly bias map (V3.1) which required a separate weekly download.

    DAILY_HTF_WINDOW = 30 satisfies extract_smc's 20-bar minimum.
    """
    MIN_SMC_BARS = 20
    bias_map = {}
    n = len(df_daily)
    if n < max(DAILY_HTF_WINDOW, MIN_SMC_BARS):
        return bias_map
    for end in range(DAILY_HTF_WINDOW, n + 1):
        slice_d = df_daily.iloc[max(0, end - DAILY_HTF_WINDOW): end].copy()
        if len(slice_d) < MIN_SMC_BARS:
            continue
        try:
            smc_d = extract_smc(slice_d, "1D")
            bias  = smc_d.get("bias", "Neutral")
        except Exception:
            bias = "Neutral"
        date_str = df_daily.index[end - 1].strftime("%Y-%m-%d")
        bias_map[date_str] = bias
    return bias_map


def _get_daily_htf_bias(bias_map: dict, signal_date: str) -> str:
    """
    Returns the most recent 30-day daily HTF bias on or before signal_date.
    Looks back up to 3 calendar days to bridge weekend/holiday gaps.
    Returns "Neutral" (conservative block) if no entry found.
    """
    if signal_date in bias_map:
        return bias_map[signal_date]
    try:
        d = dt.datetime.strptime(signal_date, "%Y-%m-%d")
        for i in range(1, 4):
            past = (d - dt.timedelta(days=i)).strftime("%Y-%m-%d")
            if past in bias_map:
                return bias_map[past]
    except Exception:
        pass
    return "Neutral"


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


def _build_ihsg_scores(period: str = BT_PERIOD) -> dict:
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
    if date_str in scores:
        return scores[date_str]
    try:
        d = dt.datetime.strptime(date_str, "%Y-%m-%d")
        for i in range(1, max_lookback + 1):
            past = (d - dt.timedelta(days=i)).strftime("%Y-%m-%d")
            if past in scores:
                return scores[past]
    except Exception:
        pass
    return None


# ── Main backtest ─────────────────────────────────────────────────────────────

def run_backtest(tickers: list = None, force: bool = False) -> dict:
    """
    Walk-forward backtest (V3).  Returns a dict with:
      trades       — list of individual trade records (all Grade A or B)
      equity_curve — cumulative R after each trade
      dd_curve     — drawdown from equity peak after each trade
      metrics      — summary statistics
      grade_stats  — per-grade breakdown (A/B/C/D)
      ticker_stats — per-ticker breakdown
      monthly_pnl  — monthly cumulative R
      params       — strategy parameters used

    All 6 entry filters must pass; only the strongest setups generate signals.
    Multi-TP partial exits: 1/3 position at TP1 (1R), TP2 (2R), TP3 (3R).
    Backtest period: 4 years of daily data.
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

    # ── Download IHSG scores ───────────────────────────────────────────────────
    ihsg_scores         = _build_ihsg_scores()
    ihsg_filtered_count = 0
    IHSG_MIN_SCORE      = 50

    # ── Batch download 4 years of daily data ──────────────────────────────────
    try:
        raw = yf.download(
            tickers_str, period=BT_PERIOD, interval="1d",
            group_by="ticker", progress=False, auto_adjust=True,
        )
    except Exception as e:
        return {"error": f"Daily data download failed: {e}"}

    # NOTE (V3.2): Weekly download removed. HTF confirmation now uses the
    # same daily data with a 30-bar rolling window (_build_daily_htf_map).
    # This eliminates a separate network request and simplifies the pipeline.

    all_trades = []
    skipped    = []
    filter_counts = {
        "ihsg": 0, "smc_bias": 0, "poi": 0, "rejection": 0,
        "htf_daily": 0, "ma20": 0, "volume": 0,
    }

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

            # Build 30-day daily HTF bias map for this ticker (no extra download)
            htf_map = _build_daily_htf_map(df) if len(df) >= DAILY_HTF_WINDOW else {}
            in_trade_until = -1

            for end in range(WINDOW, n - MAX_HOLD - 1, STEP):
                if end <= in_trade_until:
                    continue

                signal_date = dates[end - 1] if (end - 1) < n else ""

                # ── Filter 1: IHSG market-weather gate ────────────────────────
                if ihsg_scores:
                    ihsg_score = _ihsg_score_for_date(ihsg_scores, signal_date)
                    if ihsg_score is None or ihsg_score < IHSG_MIN_SCORE:
                        ihsg_filtered_count += 1
                        filter_counts["ihsg"] += 1
                        continue

                # ── Filter 2: Daily SMC bias ───────────────────────────────────
                slice_df = df.iloc[end - WINDOW: end].copy()
                smc = extract_smc(slice_df, "1D")
                if smc.get("bias") != "Bullish":
                    filter_counts["smc_bias"] += 1
                    continue

                # ── Filter 3: POI proximity ────────────────────────────────────
                curr_close = closes[end - 1]
                poi_low    = smc.get("poi_low",  0)
                poi_high   = smc.get("poi_high", 0)
                if poi_low <= 0 or poi_high <= 0:
                    filter_counts["poi"] += 1
                    continue
                if not (curr_close <= poi_high * (1 + POI_BAND)
                        and curr_close >= poi_low  * (1 - POI_BAND)):
                    filter_counts["poi"] += 1
                    continue

                # ── Filter 4: Rejection candle (buyers defending POI) ──────────
                if not _is_rejection_candle(opens, closes, highs, lows, end - 1):
                    filter_counts["rejection"] += 1
                    continue

                # ── Filter 5: 30-day Daily HTF alignment ─────────────────────
                # Uses a 30-bar rolling window on the same daily data — no extra
                # download. Replaces the weekly bias (V3.1) which needed a
                # separate weekly yfinance download.
                htf_bias = _get_daily_htf_bias(htf_map, signal_date)
                if htf_bias != "Bullish":
                    filter_counts["htf_daily"] += 1
                    continue

                # ── Filter 6: MA20 short-term uptrend (hard filter) ──────────
                # MA50 retained for grade scoring only; POI pullbacks can
                # briefly touch MA50, so MA20 is the operative gate.
                close_win = closes[end - WINDOW: end]
                ma20 = float(pd.Series(close_win).rolling(20).mean().iloc[-1])
                ma50 = float(pd.Series(close_win).rolling(50).mean().iloc[-1]) \
                       if len(close_win) >= 50 else 0.0
                trend_ma20 = bool(curr_close > ma20)
                trend_ma50 = bool(curr_close > ma50) if ma50 > 0 else False
                if not trend_ma20:
                    filter_counts["ma20"] += 1
                    continue

                # ── Filter 7: Volume confirmation (hard filter) ───────────────
                # 1.1× average — IDX pullback entries occur on moderate volume;
                # requiring 1.5× over-filtered valid institutional demand zones.
                vol_win   = vols[end - WINDOW: end]
                avg10v    = float(pd.Series(vol_win).rolling(10).mean().iloc[-1])
                vol_valid = avg10v > 0 and bool(vols[end - 1] > VOL_MULT * avg10v)
                if not vol_valid:
                    filter_counts["volume"] += 1
                    continue

                # ── All 6 filters passed — compute entry ──────────────────────
                entry_idx   = end
                entry_price = float(opens[entry_idx])
                if entry_price <= 0:
                    continue

                # ── ATR-based SL ──────────────────────────────────────────────
                atr_win = min(14, end - 1)
                if atr_win > 5:
                    tr_vals = []
                    for i in range(end - atr_win, end):
                        h_b = highs[i]; l_b = lows[i]
                        c_p = closes[i - 1] if i > 0 else closes[i]
                        tr_vals.append(max(h_b - l_b, abs(h_b - c_p), abs(l_b - c_p)))
                    atr = sum(tr_vals) / len(tr_vals) if tr_vals else 0
                else:
                    atr = 0

                sl_poi = poi_low * (1 - SL_FACTOR)
                sl_atr = (entry_price - atr * 1.5) if atr > 0 else entry_price * 0.97
                sl     = max(sl_poi, sl_atr)

                if sl >= entry_price:
                    continue

                risk     = max(entry_price - sl, 0.0001)
                risk_pct = risk / entry_price * 100

                # ── Multi-TP levels ───────────────────────────────────────────
                tp1 = entry_price + risk * TP1_R
                tp2 = entry_price + risk * TP2_R
                tp3 = entry_price + risk * TP3_R

                # ── Simulate trade with partial exits ─────────────────────────
                tp1_hit = tp2_hit = tp3_hit = False
                sl_current = sl
                exit_price = float(closes[min(entry_idx + MAX_HOLD - 1, n - 1)])
                exit_idx   = min(entry_idx + MAX_HOLD - 1, n - 1)
                outcome    = "LOSS"

                for fwd in range(entry_idx, min(entry_idx + MAX_HOLD, n)):
                    h_f = float(highs[fwd]); l_f = float(lows[fwd])

                    # Check TPs in ascending order
                    if not tp1_hit and h_f >= tp1:
                        tp1_hit    = True
                        sl_current = entry_price   # move SL to breakeven

                    if tp1_hit and not tp2_hit and h_f >= tp2:
                        tp2_hit    = True
                        sl_current = tp1           # lock in TP1

                    if tp2_hit and not tp3_hit and h_f >= tp3:
                        tp3_hit    = True
                        exit_price = tp3
                        exit_idx   = fwd
                        outcome    = "WIN"
                        break

                    # Trailing SL check
                    if l_f <= sl_current:
                        exit_price = sl_current
                        exit_idx   = fwd
                        outcome    = "WIN" if tp1_hit else "LOSS"
                        break
                else:
                    # Timeout — exit at close
                    exit_price = float(closes[min(entry_idx + MAX_HOLD - 1, n - 1)])
                    exit_idx   = min(entry_idx + MAX_HOLD - 1, n - 1)
                    outcome    = "WIN" if exit_price >= entry_price else "LOSS"

                # ── P&L in R (1/3 position at each level) ────────────────────
                if not tp1_hit:
                    # Full loss — no partial hits
                    pnl_r = (exit_price - entry_price) / risk
                elif not tp2_hit:
                    # TP1 hit; remaining 2/3 stopped at breakeven or close
                    pnl_r = ((tp1 - entry_price)
                             + (exit_price - entry_price)
                             + (exit_price - entry_price)) / (3 * risk)
                elif not tp3_hit:
                    # TP1+TP2 hit; final 1/3 at SL (TP1 level) or close
                    pnl_r = ((tp1 - entry_price)
                             + (tp2 - entry_price)
                             + (exit_price - entry_price)) / (3 * risk)
                else:
                    # All 3 TPs hit = +2.0R
                    pnl_r = ((tp1 - entry_price)
                             + (tp2 - entry_price)
                             + (tp3 - entry_price)) / (3 * risk)

                pnl_pct   = (exit_price - entry_price) / entry_price * 100
                hold_days = exit_idx - entry_idx

                flags = ["Near POI", "Rejection Candle", "HTF Bullish"]
                if trend_ma20: flags.append("Above MA20")
                if trend_ma50: flags.append("Above MA50")
                flags.append("High Vol")
                if smc.get("bos"):  flags.append("BOS")
                if smc.get("idm"):  flags.append("IDM")

                # ── Grade (proxy composite — no broker data available) ─────────
                smc_pts    = 30 if smc.get("bos") else 20
                idm_pts    = 15 if smc.get("idm") else 0
                # HTF (15), MA50 score (20 if above else 0), Vol (15), POI (25) guaranteed
                ma50_pts    = 20 if trend_ma50 else 0
                proxy_raw   = 25 + smc_pts + idm_pts + 15 + ma50_pts + 15
                proxy_score = min(100, round(proxy_raw / 1.2))
                grade = ("A" if proxy_score >= 80
                         else "B" if proxy_score >= 65
                         else "C" if proxy_score >= 50
                         else "D")

                all_trades.append({
                    "ticker":            t,
                    "entry_date":        dates[entry_idx] if entry_idx < n else "",
                    "exit_date":         dates[exit_idx]  if exit_idx  < n else "",
                    "entry_price":       round(entry_price, 2),
                    "exit_price":        round(exit_price,  2),
                    "sl":                round(sl,  2),
                    "tp":                round(tp3, 2),   # legacy — TP3 as headline
                    "tp1":               round(tp1, 2),
                    "tp2":               round(tp2, 2),
                    "tp3":               round(tp3, 2),
                    "tp1_hit":           tp1_hit,
                    "tp2_hit":           tp2_hit,
                    "tp3_hit":           tp3_hit,
                    "outcome":           outcome,
                    "pnl_r":             round(float(pnl_r),   3),
                    "pnl_pct":           round(float(pnl_pct), 2),
                    "risk_pct":          round(float(risk_pct), 2),
                    "hold_days":         int(hold_days),
                    "flags":             flags,
                    "poi_low":           round(poi_low,  2),
                    "poi_high":          round(poi_high, 2),
                    "htf_bias":          htf_bias,
                    "rejection_candle":  True,   # guaranteed by filter
                    "grade":             grade,
                    "proxy_score":       proxy_score,
                })

                in_trade_until = exit_idx

        except Exception as e:
            skipped.append(f"{t}: {str(e)[:60]}")

    # ── Sort by entry date ─────────────────────────────────────────────────────
    all_trades.sort(key=lambda x: x["entry_date"])

    # ── Aggregate metrics ──────────────────────────────────────────────────────
    wins   = [t for t in all_trades if t["outcome"] == "WIN"]
    losses = [t for t in all_trades if t["outcome"] == "LOSS"]
    total  = len(all_trades)

    win_rate      = len(wins) / total * 100 if total else 0
    total_r       = sum(t["pnl_r"] for t in all_trades)
    avg_win_r     = sum(t["pnl_r"] for t in wins)   / len(wins)   if wins   else 0
    avg_loss_r    = sum(t["pnl_r"] for t in losses) / len(losses) if losses else 0
    gross_profit  = sum(t["pnl_r"] for t in wins)   if wins   else 0
    gross_loss    = abs(sum(t["pnl_r"] for t in losses)) if losses else 0.001
    profit_factor = gross_profit / gross_loss
    expectancy    = total_r / total if total else 0
    avg_hold      = sum(t["hold_days"] for t in all_trades) / total if total else 0
    avg_pnl_pct   = sum(t["pnl_pct"]  for t in all_trades) / total if total else 0
    longest_win   = max((t["hold_days"] for t in wins),   default=0)
    longest_loss  = max((t["hold_days"] for t in losses), default=0)
    best_trade    = max(all_trades, key=lambda x: x["pnl_r"], default=None)
    worst_trade   = min(all_trades, key=lambda x: x["pnl_r"], default=None)

    eq_curve = _equity_curve(all_trades)
    dd_curve = _drawdown_series(eq_curve)
    max_dd   = max(dd_curve, default=0)

    # TP hit rates
    tp1_hits = sum(1 for t in all_trades if t.get("tp1_hit"))
    tp2_hits = sum(1 for t in all_trades if t.get("tp2_hit"))
    tp3_hits = sum(1 for t in all_trades if t.get("tp3_hit"))
    tp1_rate = round(tp1_hits / total * 100, 1) if total else 0
    tp2_rate = round(tp2_hits / total * 100, 1) if total else 0
    tp3_rate = round(tp3_hits / total * 100, 1) if total else 0

    # ── Consecutive streaks ────────────────────────────────────────────────────
    max_cw = max_cl = cur_w = cur_l = 0
    for tr in all_trades:
        if tr["outcome"] == "WIN":
            cur_w += 1; cur_l = 0
        else:
            cur_l += 1; cur_w = 0
        max_cw = max(max_cw, cur_w)
        max_cl = max(max_cl, cur_l)

    # ── Grade breakdown ────────────────────────────────────────────────────────
    grade_stats = {}
    for g in ("A", "B", "C", "D"):
        g_trades = [t for t in all_trades if t.get("grade") == g]
        g_wins   = [t for t in g_trades  if t["outcome"] == "WIN"]
        g_total  = len(g_trades)
        g_pnl_r  = sum(t["pnl_r"] for t in g_trades)
        grade_stats[g] = {
            "trades":   g_total,
            "wins":     len(g_wins),
            "losses":   g_total - len(g_wins),
            "win_rate": round(len(g_wins) / g_total * 100, 1) if g_total else 0.0,
            "total_r":  round(g_pnl_r, 2),
            "avg_r":    round(g_pnl_r / g_total, 3) if g_total else 0.0,
        }

    # ── Monthly P&L ───────────────────────────────────────────────────────────
    monthly = {}
    for tr in all_trades:
        month = tr["entry_date"][:7]
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
        "total_trades":          total,
        "wins":                  len(wins),
        "losses":                len(losses),
        "win_rate":              round(win_rate, 1),
        "total_r":               round(total_r, 2),
        "avg_win_r":             round(avg_win_r,  3),
        "avg_loss_r":            round(avg_loss_r, 3),
        "profit_factor":         round(profit_factor, 2),
        "max_drawdown_r":        round(max_dd, 2),
        "expectancy_r":          round(expectancy, 3),
        "avg_hold_days":         round(avg_hold, 1),
        "avg_pnl_pct":           round(avg_pnl_pct, 2),
        "max_consec_wins":       max_cw,
        "max_consec_losses":     max_cl,
        "tickers_tested":        len(tickers),
        "tickers_with_trades":   len(ticker_stats),
        "tickers_skipped":       len(skipped),
        "best_trade":            best_trade,
        "worst_trade":           worst_trade,
        "longest_win_days":      longest_win,
        "longest_loss_days":     longest_loss,
        "ihsg_filtered_signals": ihsg_filtered_count,
        "ihsg_filter_active":    len(ihsg_scores) > 0,
        "tp1_rate":              tp1_rate,
        "tp2_rate":              tp2_rate,
        "tp3_rate":              tp3_rate,
        "tp1_hits":              tp1_hits,
        "tp2_hits":              tp2_hits,
        "tp3_hits":              tp3_hits,
        "filter_counts":         filter_counts,
    }

    result = {
        "trades":       all_trades,
        "equity_curve": eq_curve,
        "dd_curve":     dd_curve,
        "metrics":      metrics,
        "grade_stats":  grade_stats,
        "ticker_stats": ticker_stats,
        "monthly_pnl":  monthly,
        "params": {
            "tp1_r":          TP1_R,
            "tp2_r":          TP2_R,
            "tp3_r":          TP3_R,
            "tp_note":        "Partial exits: 1/3 at TP1 (1R), 1/3 at TP2 (2R), 1/3 at TP3 (3R)",
            "sl_factor":      SL_FACTOR,
            "sl_note":        "POI-based SL with ATR-adaptive tightening",
            "max_hold":       MAX_HOLD,
            "max_hold_note":  "40 bars — allows TP3 (3R) to develop",
            "window":         WINDOW,
            "step":           STEP,
            "poi_band":       POI_BAND,
            "htf_window":     DAILY_HTF_WINDOW,
            "htf_note":       "30-day daily rolling window (no extra download)",
            "entry_filters":  "6 hard filters: IHSG + SMC Bullish + POI ±5% + Rejection Candle + 30d Daily HTF + MA20 + Vol ≥1.1×",
            "universe":       len(tickers),
            "ihsg_filter":    f"IHSG score >= {IHSG_MIN_SCORE} (Bull/Strong Bull only)",
            "period":         BT_PERIOD,
            "version":        "v3.2 — 1H screener SMC, 30d daily HTF, 10y period",
            "trade_count_note": f"{total} trades over {BT_PERIOD}" + (
                " ⚠ below 100-trade minimum — consider loosening POI_BAND or VOL_MULT"
                if total < 100 else ""
            ),
        },
    }

    _save_bt_cache(result)
    _cache_set("backtest_result", result)
    return result
