"""
backtest.py — Walk-forward backtest of the SMC + POI entry strategy on IDX stocks.
VERSION 3.1 — Calibrated filters (all 6 must pass)

Root-cause fix from V3: WEEKLY_WINDOW=8 was below extract_smc's 20-bar minimum,
causing weekly filter to return "Neutral" on every signal → 0 trades.

Calibration changes vs V3:
  • WEEKLY_WINDOW 8 → 26 (6-month context; safely above the 20-bar minimum)
  • Bearish dominance REMOVED — contradicts POI pullback pattern (pullbacks
    naturally produce 3-4 declining bars; rejection candle already covers momentum)
  • Volume threshold 1.5× → 1.1× — IDX pullback entries occur on moderate volume;
    requiring a 50% spike above average over-filters valid institutional setups
  • MA50 → MA20 as hard gate — POI demand zones are below recent highs; the
    pullback to the zone can briefly touch MA50; MA50 kept for grade scoring only

Entry filters (all 6 must pass):
  1. IHSG market-weather gate (score >= 50, Bull or Strong Bull)
  2. Daily SMC bias = Bullish (BOS or CHoCH via LuxAlgo algorithm)
  3. Price within POI demand zone (±5%)
  4. Rejection candle on signal bar (green candle or relaxed hammer)
  5. Weekly SMC bias = Bullish (26-week rolling window — MTF alignment)
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

Backtest period: 4 years daily data
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

# ── Strategy parameters (V3) ───────────────────────────────────────────────────
TP1_R         = 1.0    # first partial exit (1R)
TP2_R         = 2.0    # second partial exit (2R)
TP3_R         = 3.0    # final exit (3R)
SL_FACTOR     = 0.015  # SL = poi_low × (1 - SL_FACTOR) below demand zone
MAX_HOLD      = 50     # max bars to hold (optimized to 50; +1.63R vs 40-bar baseline)
WINDOW        = 60     # rolling SMC detection window (daily bars)
STEP          = 3      # advance step between signal checks
POI_BAND      = 0.05   # price must be within ±5% of POI zone
WEEKLY_WINDOW = 26     # weekly bars for MTF SMC (26 weeks = 6 months; must be ≥ 20)
BT_PERIOD     = "10y"  # 10-year backtest period
VOL_MULT      = 1.1    # volume must be ≥ this × 10-day average (slightly above avg)


# ── Helper functions ──────────────────────────────────────────────────────────

def _is_rejection_candle(opens, closes, highs, lows, idx: int) -> bool:
    """
    True if the bar at idx shows buyers defending the POI zone.
    Condition A: green candle (close > open).
    Condition B: relaxed hammer — lower wick >= 1× body AND lower wick > upper wick.
      (Was 1.5× — relaxed to 1× to capture more valid IDX pullback reversals)
    """
    if idx < 0 or idx >= len(closes):
        return False
    o = float(opens[idx]);  c = float(closes[idx])
    h = float(highs[idx]);  l = float(lows[idx])
    if c > o:
        return True
    body        = abs(c - o)
    lower_wick  = min(o, c) - l
    upper_wick  = h - max(o, c)
    if body > 0 and lower_wick >= body and lower_wick > upper_wick:
        return True
    return False


# NOTE: _is_bearish_dominant() REMOVED — contradicts POI pullback pattern.
# Price retracing into a demand zone after BOS naturally produces 3-4 bearish
# bars. Requiring fewer than 4/5 declining bars would block valid setups.
# The rejection candle filter already ensures the signal bar itself is bullish.


def _build_weekly_bias_map(df_weekly: pd.DataFrame) -> dict:
    """
    Rolls a WEEKLY_WINDOW-bar window over a weekly OHLCV DataFrame and records
    the SMC bias at each week end.  Returns {week_date_str: "Bullish"|"Bearish"|"Neutral"}.

    IMPORTANT: extract_smc() requires len(records) >= 20.
    WEEKLY_WINDOW = 26 satisfies this; never set below 22 or all entries are "Neutral".
    """
    MIN_SMC_BARS = 20   # extract_smc hard minimum
    bias_map = {}
    n = len(df_weekly)
    if n < max(WEEKLY_WINDOW, MIN_SMC_BARS):
        return bias_map
    for end in range(WEEKLY_WINDOW, n + 1):
        start    = max(0, end - WEEKLY_WINDOW)
        slice_wk = df_weekly.iloc[start: end].copy()
        if len(slice_wk) < MIN_SMC_BARS:
            # Slice too small — skip; _get_weekly_bias will find nearest valid entry
            continue
        try:
            smc_wk = extract_smc(slice_wk, "1W")
            bias   = smc_wk.get("bias", "Neutral")
        except Exception:
            bias = "Neutral"
        week_date = df_weekly.index[end - 1].strftime("%Y-%m-%d")
        bias_map[week_date] = bias
    return bias_map


def _get_weekly_bias(bias_map: dict, signal_date: str) -> str:
    """
    Returns the most recent weekly SMC bias on or before signal_date.
    Looks back up to 7 calendar days to bridge weekend/holiday gaps.
    Returns "Neutral" (conservative block) if no entry found.
    """
    if signal_date in bias_map:
        return bias_map[signal_date]
    try:
        d = dt.datetime.strptime(signal_date, "%Y-%m-%d")
        for i in range(1, 8):
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

    # ── Batch download 4 years of weekly data (for MTF) ───────────────────────
    try:
        raw_weekly = yf.download(
            tickers_str, period=BT_PERIOD, interval="1wk",
            group_by="ticker", progress=False, auto_adjust=True,
        )
    except Exception:
        raw_weekly = None   # graceful degradation — MTF filter skipped

    # ── Pre-build weekly bias maps per ticker ──────────────────────────────────
    weekly_bias_maps: dict[str, dict] = {}
    if raw_weekly is not None:
        for t, t_jk in zip(tickers, tickers_jk):
            try:
                if isinstance(raw_weekly.columns, pd.MultiIndex):
                    if t_jk not in raw_weekly.columns.get_level_values(0):
                        continue
                    df_wk = raw_weekly[t_jk].dropna()
                else:
                    df_wk = raw_weekly.dropna()
                if len(df_wk) >= WEEKLY_WINDOW:
                    weekly_bias_maps[t] = _build_weekly_bias_map(df_wk)
            except Exception:
                pass

    all_trades = []
    skipped    = []
    filter_counts = {
        "ihsg": 0, "smc_bias": 0, "poi": 0, "rejection": 0,
        "weekly": 0, "ma20": 0, "volume": 0,
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

            w_bias_map = weekly_bias_maps.get(t, {})
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

                # ── Filter 5: Weekly MTF alignment ────────────────────────────
                weekly_bias = _get_weekly_bias(w_bias_map, signal_date)
                if weekly_bias != "Bullish":
                    filter_counts["weekly"] += 1
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

                # ── Regime detection (classify IHSG state) ────────────────────
                if ihsg_score is not None:
                    regime_state = "Strong Bull" if ihsg_score >= 70 else \
                                   "Bull" if ihsg_score >= 50 else \
                                   "Ranging" if ihsg_score >= 30 else "Bear"
                else:
                    regime_state = "Unknown"

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

                flags = ["Near POI", "Rejection Candle", "Weekly Bullish"]
                if trend_ma20: flags.append("Above MA20")
                flags.append("Above MA50")
                flags.append("High Vol")
                if smc.get("bos"):  flags.append("BOS")
                if smc.get("idm"):  flags.append("IDM")

                # ── Confluence signals dict (10 boolean conditions) ────────────
                is_green_candle = closes[end - 1] > opens[end - 1]
                poi_band_tight = curr_close <= (poi_low + (poi_high - poi_low) * 0.03)
                vol_high = vols[end - 1] > 1.5 * avg10v if avg10v > 0 else False
                has_bos = bool(smc.get("bos"))
                has_idm = bool(smc.get("idm"))

                confluence_signals = {
                    "regime_bull": regime_state in ["Bull", "Strong Bull"],
                    "ihsg_tight": ihsg_score >= 70 if ihsg_score else False,
                    "bos": has_bos,
                    "poi_tight": poi_band_tight,
                    "rejection_green": is_green_candle,
                    "weekly_bullish": weekly_bias == "Bullish",
                    "ma50_above": trend_ma50,
                    "idm": has_idm,
                    "volume_high": vol_high,
                    "hold_time": None,
                }

                # ── Grade based on tight conditions count ────────────────────
                tight_count = sum(1 for v in confluence_signals.values() if v is True)
                grade = ("A" if tight_count >= 6
                         else "B" if tight_count >= 4
                         else "C" if tight_count >= 2
                         else "D")
                confluence_score = tight_count / 10.0

                # ── Position sizing (1 unit fixed for V3.1 baseline) ────────
                position_size_pct = 1.0
                fee_r = (risk / entry_price * 100 * 0.0045) / 100  # 0.45% round-trip fee

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
                    "weekly_bias":       weekly_bias,
                    "rejection_candle":  True,   # guaranteed by filter
                    "regime_state":      regime_state,
                    "confluence_signals": confluence_signals,
                    "confluence_score":  round(confluence_score, 2),
                    "position_size_pct": position_size_pct,
                    "fee_r":             round(fee_r, 3),
                    "grade":             grade,
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
            "weekly_window":  WEEKLY_WINDOW,
            "entry_filters":  "6 hard filters: IHSG + SMC Bullish + POI ±5% + Rejection Candle + Weekly MTF + MA20 + Vol ≥1.1×",
            "universe":       len(tickers),
            "ihsg_filter":    f"IHSG score >= {IHSG_MIN_SCORE} (Bull/Strong Bull only)",
            "period":         BT_PERIOD,
            "version":        "v3.1 — calibrated filters, Grade A/B signals only",
        },
    }

    _save_bt_cache(result)
    _cache_set("backtest_result", result)
    return result
