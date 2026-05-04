"""
backtest.py — Walk-forward backtest of the SMC + POI entry strategy on IDX stocks.
VERSION 3.9 — Markov Regime · Elliott Wave · Confluence Scoring · Risk-Scaled Sizing

V3.9 enhancements vs V3.8 (applying trading-signals-skill methodologies):
  • Markov Regime pre-filter: classifies market into 7 states from IHSG OHLCV.
    Bull Quiet / Bull Volatile / Ranging → trade.
    Ranging Vol / Bear Quiet / Bear Volatile / Crisis → skip entirely.
  • Regime-specific TP targets and SL:
    Bull Quiet:   TP1=1.0R  TP2=2.5R  TP3=4.0R  pos=1.0x
    Bull Volatile: TP1=0.8R TP2=2.0R  TP3=3.5R  pos=0.8x
    Ranging:       TP1=0.6R TP2=1.5R  TP3=2.5R  pos=0.5x (only if confluence≥0.70)
  • Elliott Wave confirmation: +1 to confluence if price in Wave 3 or Wave 5 (trending).
  • Confluence scoring (V3.9 gate): 10 binary signals summed → 0.0–1.0.
    ≥0.70 → execute | 0.50–0.69 → wait/reduce | <0.50 → skip.
    Replaces bonus-point grade system as the entry gate.
  • Risk-scaled position sizing: position_size = confluence × regime_multiplier.
    pnl_r in trade dict is gross R × position_size (reflects actual capital deployed).
  • Grade from confluence: A ≥ 0.80 | B ≥ 0.60 | C ≥ 0.40 | D < 0.40.
  • Raw data cache (determinism fix): OHLCV downloaded once, saved to disk for 7 days.
    All runs (including force=True) use same underlying data → consistent results.

V3.8 retained (unchanged from V3.9 perspective):
  • Asymmetric TP split: 1/2 at TP1, 1/4 at TP2, 1/4 at TP3
  • 8 hard entry filters (IHSG, SMC, 40d trend, 4H POI, HTF, Dual MA20, Volume)
  • Wyckoff and Rejection candle as quality signals (now feed confluence)

10 Confluence signals (each 0 or 1):
  1. Regime favorable (Bull Quiet or Bull Volatile)
  2. IHSG 40-day trend up
  3. Stock 40-day trend up
  4. SMC BOS confirmed
  5. Wyckoff accumulation detected
  6. Elliott Wave 3 or 5 (trending wave)
  7. IDM confirmed (smart money sweep)
  8. Rejection candle present
  9. MA50 above price
  10. 4H-proxy SMC bias Bullish (20-bar)

Timeframe note: ALL bars are DAILY (1D) — "4H proxy" = shorter sub-window of daily bars.
  WINDOW=60 daily bars ≈ 3 months (macro trend / SMC bias)
  WYCKOFF_WINDOW=45 daily bars ≈ 9 weeks (4H POI zone + Wyckoff analysis)
  DAILY_HTF_WINDOW=30 daily bars ≈ 6 weeks (medium-term HTF context)

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
    _is_rejection_candle,    # shared helper — moved to analysis.py in V3.2
    _download_chunked,       # chunked batch downloader — avoids IDX throttle warnings
)

BASE_DIR      = Path(__file__).resolve().parent.parent
BT_CACHE_FILE = BASE_DIR / "backtest_cache.json"
BT_CACHE_TTL  = 86400   # 24 hours

# ── Strategy parameters (V3.9) ────────────────────────────────────────────────
# V3.8 base TP constants (used as default; regime overrides per-trade below)
TP1_R            = 1.0    # Bull Quiet default — 1/2 position
TP2_R            = 2.5
TP3_R            = 4.0
SL_FACTOR        = 0.015  # SL = poi_low × (1 - SL_FACTOR) below demand zone
MAX_HOLD         = 60     # max bars to hold
WINDOW           = 60     # rolling SMC detection window (daily bars ≈ 3 months)
STEP             = 3      # advance step between signal checks
POI_BAND         = 0.07   # ±7% POI proximity band
WYCKOFF_WINDOW   = 45     # 4H-proxy sub-window (daily bars ≈ 9 weeks)
DAILY_HTF_WINDOW = 30     # HTF window (daily bars ≈ 6 weeks)
BT_PERIOD        = "10y"  # 10-year backtest period
VOL_MULT         = 1.1    # volume ≥ this × 10-day average
REGIME_WINDOW    = 60     # bars used for Markov regime detection (matches WINDOW)
CONFLUENCE_GATE  = 0.70   # minimum confluence score to enter trade

# Regime → TP multipliers, SL factor, position sizing
# pos: multiplied against confluence to get final position_size
REGIME_CONFIG = {
    "Bull Quiet":    {"tp1": 1.0, "tp2": 2.5, "tp3": 4.0, "sl": 0.015, "pos": 1.0},
    "Bull Volatile": {"tp1": 0.8, "tp2": 2.0, "tp3": 3.5, "sl": 0.013, "pos": 0.8},
    "Ranging":       {"tp1": 0.6, "tp2": 1.5, "tp3": 2.5, "sl": 0.010, "pos": 0.5},
}
HOSTILE_REGIMES  = {"Ranging Vol", "Bear Quiet", "Bear Volatile", "Crisis"}
TRADEABLE_REGIMES = set(REGIME_CONFIG.keys())


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


# ── Raw data cache (determinism fix) ─────────────────────────────────────────
# Caches the raw yfinance OHLCV download to disk for 7 days.
# yfinance can return slightly different adjusted prices on each call (due to
# ongoing corporate-action adjustments). Using the same cached data on all runs
# — including force=True re-runs — guarantees identical results.
BT_DATA_CACHE_FILE = BASE_DIR / "backtest_data_cache.pkl"
BT_DATA_CACHE_TTL  = 7 * 86400   # 7 days


def _load_bt_raw_cache():
    if BT_DATA_CACHE_FILE.exists():
        try:
            if (time.time() - BT_DATA_CACHE_FILE.stat().st_mtime) < BT_DATA_CACHE_TTL:
                import pickle
                with open(BT_DATA_CACHE_FILE, "rb") as f:
                    return pickle.load(f)
        except Exception:
            pass
    return None


def _save_bt_raw_cache(payload: dict):
    try:
        import pickle
        with open(BT_DATA_CACHE_FILE, "wb") as f:
            pickle.dump(payload, f, protocol=4)
    except Exception:
        pass


# ── V3.9 Helper: Markov Regime Detection ─────────────────────────────────────

def _compute_markov_regime(closes):
    """
    Classify market regime from a slice of daily closes (REGIME_WINDOW bars).
    Returns (regime_state: str, regime_confidence: float 0–1).

    States: Bull Quiet, Bull Volatile, Ranging, Ranging Vol,
            Bear Quiet, Bear Volatile, Crisis

    Metrics used:
      - returns_std: daily return std dev (%)
      - rsi14:       14-bar RSI of closes
      - trend:       second-half mean vs first-half mean of window
    """
    if len(closes) < 20:
        return "Ranging", 0.30

    arr = np.asarray(closes, dtype=float)

    # Daily return std dev (%)
    returns     = np.diff(arr) / np.where(arr[:-1] > 0, arr[:-1], 1.0)
    returns_std = float(np.std(returns) * 100)

    # 14-bar RSI
    def _rsi14(p):
        if len(p) < 15:
            return 50.0
        g, l = [], []
        for i in range(1, len(p)):
            d = p[i] - p[i - 1]
            g.append(max(d, 0.0))
            l.append(max(-d, 0.0))
        ag = sum(g[-14:]) / 14.0
        al = sum(l[-14:]) / 14.0
        return 100.0 if al == 0 else 100.0 - 100.0 / (1.0 + ag / al)

    rsi = _rsi14(list(arr))

    # Trend: mean of second half vs first half
    h         = max(len(arr) // 2, 1)
    ma_early  = float(np.mean(arr[:h]))
    ma_late   = float(np.mean(arr[h:]))
    trend_up   = ma_late > ma_early * 1.02
    trend_down = ma_late < ma_early * 0.98

    # Classification (priority order — most extreme first)
    if returns_std > 3.5:
        return "Crisis",       0.90
    if trend_down and returns_std > 2.5:
        return "Bear Volatile", 0.85
    if trend_down and rsi < 40:
        return "Bear Quiet",   0.80
    if not trend_up and not trend_down and returns_std > 2.5:
        return "Ranging Vol",  0.75
    if trend_up and returns_std > 2.0 and rsi > 60:
        return "Bull Volatile", 0.80
    if trend_up and returns_std <= 2.0 and 35 <= rsi <= 72:
        return "Bull Quiet",   0.85
    if 38 <= rsi <= 62:
        return "Ranging",      0.70
    # Fallback
    if trend_up:   return "Bull Quiet",  0.55
    if trend_down: return "Bear Quiet",  0.55
    return "Ranging", 0.50


# ── V3.9 Helper: Elliott Wave Position Detector ───────────────────────────────

def _detect_elliott_wave(closes):
    """
    Detect if the current price position suggests Wave 3 or Wave 5 (high-probability
    trending waves) vs Wave 2 or Wave 4 (corrective, lower probability).

    Returns (wave_3_or_5: bool, wave_label: str).

    Approach:
      - Find local swing highs and lows with 3-bar confirmation (no lookahead).
      - In bullish context: require ascending swing highs AND ascending swing lows.
      - If price is in a new-high thrust phase → label as Wave 3 or Wave 5.
    """
    n = len(closes)
    if n < 20:
        return False, "unclear"

    arr      = np.asarray(closes, dtype=float)
    lookback = max(3, n // 15)

    # Confirmed swing points (bar i is a high/low only if it's extreme in [i-lb, i+lb])
    swing_highs, swing_lows = [], []
    for i in range(lookback, n - lookback):
        window = arr[i - lookback: i + lookback + 1]
        if arr[i] == window.max():
            swing_highs.append((i, float(arr[i])))
        if arr[i] == window.min():
            swing_lows.append((i,  float(arr[i])))

    if len(swing_highs) < 2 or len(swing_lows) < 2:
        return False, "unclear"

    h1, h2 = swing_highs[-2][1], swing_highs[-1][1]
    l1, l2 = swing_lows[-2][1],  swing_lows[-1][1]

    if h2 <= h1:
        # Descending highs = correction (Wave 2 or 4)
        return False, "wave_correction"

    # Ascending highs confirmed; check if current price is in a thrust phase
    price_now    = float(arr[-1])
    wave1_extent = max(h1 - l1, 0.001)   # approximate Wave 1 height

    if l2 > l1:  # ascending lows (impulse structure confirmed)
        if price_now >= h1 + wave1_extent * 0.5:
            # Strong thrust — Wave 3 (most powerful, 1.618× wave 1) or Wave 5
            label = "wave_3" if h2 > h1 * 1.015 else "wave_5"
            return True, label
        else:
            return False, "wave_4"   # consolidating above prior high = wave 4

    # Weak ascending (only highs ascending, lows flat/down) = early wave
    if price_now > h1:
        return True, "wave_3_or_5"

    return False, "wave_2_or_unclear"


# ── V3.9 Helper: Confluence Score ─────────────────────────────────────────────

def _confluence_score(
    regime_favorable: bool,
    ihsg_40d_up:      bool,
    stock_40d_up:     bool,
    bos:              bool,
    wyckoff_accum:    bool,
    wave_3_or_5:      bool,
    idm:              bool,
    rejection_candle: bool,
    ma50_above:       bool,
    trend_4h:         bool,
) -> float:
    """
    V3.9 confluence score: sum of 10 binary signals / 10 → 0.0–1.0.

    Entry gate (in run_backtest):
      ≥ 0.70 → EXECUTE  (7/10 signals confirmed)
      < 0.70 → SKIP     (insufficient confluence)

    Grade:
      A ≥ 0.80 | B ≥ 0.60 | C ≥ 0.40 | D < 0.40
    """
    signals = [
        regime_favorable, ihsg_40d_up,   stock_40d_up, bos,
        wyckoff_accum,    wave_3_or_5,   idm,          rejection_candle,
        ma50_above,       trend_4h,
    ]
    return round(sum(1 for s in signals if s) / 10.0, 2)


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


def _build_jkse_data(period: str = BT_PERIOD) -> tuple[dict, dict]:
    """
    Downloads ^JKSE once and returns BOTH:
      ihsg_scores: {date_str: int}                      — 0/30/60/70/100 MA-score
      regime_map:  {date_str: (state: str, conf: float)} — Markov 7-state classification

    Single download → no duplicate yfinance calls.
    """
    try:
        df = yf.download("^JKSE", period=period, interval="1d",
                         progress=False, auto_adjust=True)
        if df.empty:
            return {}, {}
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        close = df["Close"].astype(float)
        ma20  = close.rolling(20).mean()
        ma50  = close.rolling(50).mean()
        ma200 = close.rolling(200).mean()

        closes_arr = close.values
        dates_arr  = df.index.strftime("%Y-%m-%d").tolist()
        n          = len(closes_arr)

        # IHSG scores
        scores = {}
        for i in range(n):
            date_str = dates_arr[i]
            c    = float(close.iloc[i])
            m20  = float(ma20.iloc[i])  if not pd.isna(ma20.iloc[i])  else 0.0
            m50  = float(ma50.iloc[i])  if not pd.isna(ma50.iloc[i])  else 0.0
            m200 = float(ma200.iloc[i]) if not pd.isna(ma200.iloc[i]) else 0.0
            score = 0
            if m20  > 0 and c > m20:  score += 30
            if m50  > 0 and c > m50:  score += 30
            if m200 > 0 and c > m200: score += 40
            scores[date_str] = score

        # Regime map (rolling REGIME_WINDOW bars)
        regime_map = {}
        for end in range(REGIME_WINDOW, n + 1):
            w = closes_arr[end - REGIME_WINDOW: end]
            state, conf      = _compute_markov_regime(w)
            regime_map[dates_arr[end - 1]] = (state, conf)

        return scores, regime_map
    except Exception:
        return {}, {}


def _regime_for_date(regime_map: dict, date_str: str, max_lookback: int = 5):
    """Return (state, conf) for date_str, looking back for weekends/holidays."""
    if date_str in regime_map:
        return regime_map[date_str]
    try:
        d = dt.datetime.strptime(date_str, "%Y-%m-%d")
        for i in range(1, max_lookback + 1):
            past = (d - dt.timedelta(days=i)).strftime("%Y-%m-%d")
            if past in regime_map:
                return regime_map[past]
    except Exception:
        pass
    return ("Ranging", 0.30)   # conservative default


# ── Main backtest ─────────────────────────────────────────────────────────────

def run_backtest(tickers: list = None, force: bool = False) -> dict:
    """
    Walk-forward backtest V3.9 — Markov Regime + Elliott Wave + Confluence Gate.

    Returns a dict with:
      trades        — trade records (confluence ≥ 0.70, regime tradeable)
      equity_curve  — cumulative R after each trade
      dd_curve      — drawdown from equity peak
      metrics       — summary statistics (incl. confluence_avg, regime_distribution)
      grade_stats   — per-grade breakdown (A/B/C/D from confluence)
      ticker_stats  — per-ticker breakdown
      monthly_pnl   — monthly cumulative R
      params        — strategy parameters used

    V3.9 flow:
      Pre-filter: Markov regime check (hostile regimes skipped entirely)
      8 hard filters: IHSG, SMC, 40d trend, 4H POI, HTF, Dual MA20, Volume
      10-signal confluence gate (≥ 0.70 to enter)
      Regime-specific TP targets + position sizing (confluence × regime_pos)
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

    tickers_jk = [f"{t}.JK" for t in tickers]

    # ── Raw data cache (DETERMINISM FIX) ──────────────────────────────────────
    # Load pre-downloaded data if < 7 days old. This ensures every run — even
    # force=True — uses the SAME price data, eliminating yfinance variability.
    IHSG_MIN_SCORE      = 50
    ihsg_filtered_count = 0

    raw_cache = _load_bt_raw_cache()
    if raw_cache:
        raw         = raw_cache["raw"]
        ihsg_scores = raw_cache["ihsg_scores"]
        regime_map  = raw_cache["regime_map"]
    else:
        # Fresh download: IHSG scores + regime map (single ^JKSE call)
        ihsg_scores, regime_map = _build_jkse_data()
        try:
            raw = _download_chunked(tickers_jk, period=BT_PERIOD, interval="1d")
            if raw.empty:
                return {"error": "Daily data download returned no data"}
        except Exception as e:
            return {"error": f"Daily data download failed: {e}"}
        _save_bt_raw_cache({
            "raw":         raw,
            "ihsg_scores": ihsg_scores,
            "regime_map":  regime_map,
        })

    all_trades = []
    skipped    = []
    filter_counts = {
        "regime":          0,   # V3.9: hostile regime (Bear/Crisis/Ranging Vol)
        "ihsg":            0,
        "ihsg_40d":        0,
        "smc_bias":        0,
        "stock_trend_40d": 0,
        "poi":             0,
        "htf_daily":       0,
        "trend":           0,
        "volume":          0,
        "confluence":      0,   # V3.9: insufficient confluence (< 0.70)
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

                # ── V3.9 Pre-filter: Markov Regime gate ───────────────────────
                # Must run BEFORE the 8 hard filters — skips hostile regimes entirely.
                regime_state, regime_conf = _regime_for_date(regime_map, signal_date)
                if regime_state in HOSTILE_REGIMES:
                    filter_counts["regime"] += 1
                    continue
                rc = REGIME_CONFIG.get(regime_state, REGIME_CONFIG["Bull Quiet"])

                # ── Filter 1: IHSG daily gate ─────────────────────────────────
                if ihsg_scores:
                    daily_score = _ihsg_score_for_date(ihsg_scores, signal_date)
                    if daily_score is None or daily_score < IHSG_MIN_SCORE:
                        ihsg_filtered_count += 1
                        filter_counts["ihsg"] += 1
                        continue

                # ── Filter 1.5: IHSG 40-day sustained bearish gate ────────────
                ihsg_40d_ok = True  # track for confluence; set False if gate trips
                if ihsg_scores:
                    try:
                        sig_date_obj = dt.date.fromisoformat(signal_date)
                        bearish_days = 0
                        counted_days = 0
                        for _d in range(1, 57):
                            past = (sig_date_obj - dt.timedelta(days=_d)).isoformat()
                            sc   = _ihsg_score_for_date(ihsg_scores, past)
                            if sc is not None:
                                counted_days += 1
                                if sc < IHSG_MIN_SCORE:
                                    bearish_days += 1
                            if counted_days >= 40:
                                break
                        if counted_days >= 20 and (bearish_days / counted_days) > 0.5:
                            filter_counts["ihsg_40d"] += 1
                            ihsg_40d_ok = False
                            continue
                    except Exception:
                        pass

                # ── Filter 2: Daily SMC bias (60-bar window) ──────────────────
                slice_df = df.iloc[end - WINDOW: end].copy()
                smc = extract_smc(slice_df, "1D")
                if smc.get("bias") != "Bullish":
                    filter_counts["smc_bias"] += 1
                    continue

                # ── Filter 2.5: Per-stock 40-day bearish trend gate ───────────
                stock_40d_ok = True
                if end >= 45:
                    close_40d    = float(closes[end - 40])
                    close_now    = float(closes[end - 1])
                    ma20_now     = float(pd.Series(closes[end - 20: end]).mean())
                    ma20_40d     = float(pd.Series(closes[end - 60: end - 40]).mean()) \
                                   if end >= 60 else float(pd.Series(closes[:end - 40]).mean())
                    stock_bearish = (close_now < close_40d) and (ma20_now < ma20_40d)
                    if stock_bearish:
                        filter_counts["stock_trend_40d"] += 1
                        stock_40d_ok = False
                        continue

                # ── 4H proxy SMC (45-bar) — for Filter 3 POI + Wyckoff ────────
                smc_wyckoff = {}
                if end >= WYCKOFF_WINDOW:
                    try:
                        smc_wyckoff = extract_smc(
                            df.iloc[end - WYCKOFF_WINDOW: end].copy(), "1D"
                        )
                    except Exception:
                        pass

                # ── Filter 3: POI proximity — 4H proxy (45-bar) ──────────────
                curr_close = closes[end - 1]
                poi_low    = smc_wyckoff.get("poi_low",  0)
                poi_high   = smc_wyckoff.get("poi_high", 0)
                if poi_low <= 0 or poi_high <= 0:
                    filter_counts["poi"] += 1
                    continue
                if not (curr_close <= poi_high * (1 + POI_BAND)
                        and curr_close >= poi_low  * (1 - POI_BAND)):
                    filter_counts["poi"] += 1
                    continue

                # ── Wyckoff accumulation (confluence signal) ──────────────────
                swing_lows_w   = smc_wyckoff.get("swing_lows",   [])
                sweep_events_w = smc_wyckoff.get("sweep_events", [])
                higher_low = (len(swing_lows_w) >= 2
                              and swing_lows_w[-1]["price"] > swing_lows_w[-2]["price"])
                has_spring = any(e.get("type") == "bear_sweep" for e in sweep_events_w)
                vol_declining_at_lows = False
                try:
                    vol_slice = vols[end - WYCKOFF_WINDOW: end]
                    if len(swing_lows_w) >= 2:
                        idx_prev = swing_lows_w[-2].get("idx", 0)
                        idx_last = swing_lows_w[-1].get("idx", 0)
                        if idx_prev < len(vol_slice) and idx_last < len(vol_slice):
                            vol_declining_at_lows = (
                                float(vol_slice[idx_last]) <= float(vol_slice[idx_prev])
                            )
                except Exception:
                    pass
                wyckoff_accum = (higher_low or has_spring) and vol_declining_at_lows

                # ── Rejection candle (confluence signal) ──────────────────────
                rejection_candle = _is_rejection_candle(opens, closes, highs, lows, end - 1)

                # ── Filter 5: 30-day Daily HTF alignment ──────────────────────
                htf_bias = _get_daily_htf_bias(htf_map, signal_date)
                if htf_bias != "Bullish":
                    filter_counts["htf_daily"] += 1
                    continue

                # ── Filter 6: Dual MA20 trend gate ────────────────────────────
                close_win  = closes[end - WINDOW: end]
                ma20_daily = float(pd.Series(close_win).rolling(20).mean().iloc[-1])
                trend_ma20 = bool(curr_close > ma20_daily)
                close_sub     = closes[end - 30: end] if end >= 30 else close_win
                ma20_sub      = float(pd.Series(close_sub).rolling(20).mean().iloc[-1]) \
                                if len(close_sub) >= 20 else 0.0
                trend_4h_ma20 = bool(curr_close > ma20_sub) if ma20_sub > 0 else False
                if not (trend_ma20 and trend_4h_ma20):
                    filter_counts["trend"] += 1
                    continue

                # MA50 (confluence signal — not a hard gate)
                ma50       = float(pd.Series(close_win).rolling(50).mean().iloc[-1]) \
                             if len(close_win) >= 50 else 0.0
                trend_ma50 = bool(curr_close > ma50) if ma50 > 0 else False

                # ── Filter 7: Volume confirmation ─────────────────────────────
                vol_win   = vols[end - WINDOW: end]
                avg10v    = float(pd.Series(vol_win).rolling(10).mean().iloc[-1])
                vol_valid = avg10v > 0 and bool(vols[end - 1] > VOL_MULT * avg10v)
                if not vol_valid:
                    filter_counts["volume"] += 1
                    continue

                # ── All 8 hard filters passed ─────────────────────────────────
                # Now compute V3.9 confluence signals before the gate.

                # 4H SMC bias (20-bar sub-window) — for confluence signal 10
                smc_4h_approx = {}
                try:
                    smc_4h_approx = extract_smc(df.iloc[end - 20: end].copy(), "1D")
                except Exception:
                    pass
                trend_4h_approx = smc_4h_approx.get("bias") == "Bullish"

                # Elliott Wave detection (confluence signal 6)
                wave_3_or_5, wave_count = _detect_elliott_wave(
                    closes[end - min(30, end): end]
                )

                # V3.9 Confluence score (10 binary signals / 10)
                # Signals 1-3 are guaranteed True for any trade reaching here:
                #   1. regime_favorable = True (passed regime gate above)
                #   2. ihsg_40d_ok = True (passed Filter 1.5)
                #   3. stock_40d_ok = True (passed Filter 2.5)
                confluence = _confluence_score(
                    regime_favorable = True,   # guaranteed (regime gate above)
                    ihsg_40d_up      = True,   # guaranteed (Filter 1.5)
                    stock_40d_up     = True,   # guaranteed (Filter 2.5)
                    bos              = bool(smc.get("bos")),
                    wyckoff_accum    = wyckoff_accum,
                    wave_3_or_5      = wave_3_or_5,
                    idm              = bool(smc.get("idm", 0)),
                    rejection_candle = rejection_candle,
                    ma50_above       = trend_ma50,
                    trend_4h         = trend_4h_approx,
                )

                # V3.9 Confluence gate: minimum 0.70 (7/10 signals)
                if confluence < CONFLUENCE_GATE:
                    filter_counts["confluence"] += 1
                    continue

                # ── Entry computation ─────────────────────────────────────────
                entry_idx   = end
                entry_price = float(opens[entry_idx])
                if entry_price <= 0:
                    continue

                # ATR-based SL
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

                sl_factor_r = rc["sl"]   # regime-specific SL factor
                sl_poi  = poi_low * (1 - sl_factor_r)
                sl_atr  = (entry_price - atr * 1.5) if atr > 0 else entry_price * 0.97
                sl      = max(sl_poi, sl_atr)
                if sl >= entry_price:
                    continue

                risk     = max(entry_price - sl, 0.0001)
                risk_pct = risk / entry_price * 100

                # ── V3.9: Regime-specific TP targets ─────────────────────────
                tp1 = entry_price + risk * rc["tp1"]
                tp2 = entry_price + risk * rc["tp2"]
                tp3 = entry_price + risk * rc["tp3"]

                # ── V3.9: Position sizing = confluence × regime_pos ───────────
                # Caps at 1.0 (never over-size).
                position_size = min(1.0, round(confluence * rc["pos"], 3))

                # ── Simulate trade with 3 partial exits ──────────────────────
                tp1_hit = tp2_hit = tp3_hit = False
                sl_current = sl
                exit_price = float(closes[min(entry_idx + MAX_HOLD - 1, n - 1)])
                exit_idx   = min(entry_idx + MAX_HOLD - 1, n - 1)
                outcome    = "LOSS"

                for fwd in range(entry_idx, min(entry_idx + MAX_HOLD, n)):
                    h_f = float(highs[fwd]); l_f = float(lows[fwd])
                    if not tp1_hit and h_f >= tp1:
                        tp1_hit    = True
                        sl_current = entry_price   # SL → breakeven
                    if tp1_hit and not tp2_hit and h_f >= tp2:
                        tp2_hit    = True
                        sl_current = tp1           # SL → TP1
                    if tp2_hit and not tp3_hit and h_f >= tp3:
                        tp3_hit    = True
                        exit_price = tp3
                        exit_idx   = fwd
                        outcome    = "WIN"
                        break
                    if l_f <= sl_current:
                        exit_price = sl_current
                        exit_idx   = fwd
                        outcome    = "WIN" if tp1_hit else "LOSS"
                        break
                else:
                    exit_price = float(closes[min(entry_idx + MAX_HOLD - 1, n - 1)])
                    exit_idx   = min(entry_idx + MAX_HOLD - 1, n - 1)
                    outcome    = "WIN" if exit_price >= entry_price else "LOSS"

                # ── Gross P&L in R (regime-specific TP ratios, 2+1+1 units) ──
                #   No TP:      −1.0R
                #   TP1 only:   (2×tp1_r + 2×0) / 4
                #   TP1+TP2:    (2×tp1_r + tp2_r + tp1_r) / 4    [SL trails to TP1]
                #   All 3:      (2×tp1_r + tp2_r + tp3_r) / 4
                if not tp1_hit:
                    pnl_r_gross = (exit_price - entry_price) / risk
                elif not tp2_hit:
                    pnl_r_gross = (2*(tp1-entry_price) + 2*(exit_price-entry_price)) / (4*risk)
                elif not tp3_hit:
                    pnl_r_gross = (2*(tp1-entry_price) + (tp2-entry_price)
                                   + (exit_price-entry_price)) / (4*risk)
                else:
                    pnl_r_gross = (2*(tp1-entry_price) + (tp2-entry_price)
                                   + (tp3-entry_price)) / (4*risk)

                # V3.9: scale P&L by actual position size
                pnl_r     = pnl_r_gross * position_size
                pnl_pct   = (exit_price - entry_price) / entry_price * 100
                hold_days = exit_idx - entry_idx

                # ── V3.9 Grade from confluence ────────────────────────────────
                # Replaces V3.8 bonus-point system. All entering trades have
                # confluence ≥ 0.70, so the practical range here is 0.70–1.00.
                grade = ("A" if confluence >= 0.80
                         else "B" if confluence >= 0.60
                         else "C" if confluence >= 0.40
                         else "D")

                flags = ["Near 4H POI", "HTF Bullish", "Dual MA20", regime_state]
                if rejection_candle:    flags.append("Rejection")
                if wyckoff_accum:       flags.append("Wyckoff")
                if trend_ma50:          flags.append("MA50")
                if wave_3_or_5:         flags.append(f"Wave {wave_count}")
                if smc.get("bos"):      flags.append("BOS")
                if smc.get("idm"):      flags.append("IDM")

                all_trades.append({
                    "ticker":           t,
                    "entry_date":       dates[entry_idx] if entry_idx < n else "",
                    "exit_date":        dates[exit_idx]  if exit_idx  < n else "",
                    "entry_price":      round(entry_price, 2),
                    "exit_price":       round(exit_price,  2),
                    "sl":               round(sl,  2),
                    "tp":               round(tp3, 2),
                    "tp1":              round(tp1, 2),
                    "tp2":              round(tp2, 2),
                    "tp3":              round(tp3, 2),
                    "tp1_hit":          tp1_hit,
                    "tp2_hit":          tp2_hit,
                    "tp3_hit":          tp3_hit,
                    "outcome":          outcome,
                    "pnl_r":            round(float(pnl_r),       3),
                    "pnl_r_gross":      round(float(pnl_r_gross), 3),
                    "pnl_pct":          round(float(pnl_pct),     2),
                    "risk_pct":         round(float(risk_pct),    2),
                    "hold_days":        int(hold_days),
                    "flags":            flags,
                    "poi_low":          round(poi_low,  2),
                    "poi_high":         round(poi_high, 2),
                    "htf_bias":         htf_bias,
                    # V3.9 fields
                    "regime_state":     regime_state,
                    "regime_conf":      round(regime_conf, 2),
                    "confluence_score": confluence,
                    "position_size_pct": round(position_size * 100, 1),
                    "wave_count":       wave_count,
                    "rejection_candle": rejection_candle,
                    "wyckoff_accum":    wyckoff_accum,
                    "grade":            grade,
                    # Confluence signal breakdown (for analysis)
                    "confluence_signals": {
                        "regime":    True,
                        "ihsg_40d":  True,
                        "stock_40d": True,
                        "bos":       bool(smc.get("bos")),
                        "wyckoff":   wyckoff_accum,
                        "wave":      wave_3_or_5,
                        "idm":       bool(smc.get("idm", 0)),
                        "rejection": rejection_candle,
                        "ma50":      trend_ma50,
                        "trend_4h":  trend_4h_approx,
                    },
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

    # V3.9: Confluence signal hit rates
    wyckoff_rate   = round(sum(1 for t in all_trades if t.get("wyckoff_accum"))    / total * 100, 1) if total else 0
    rejection_rate = round(sum(1 for t in all_trades if t.get("rejection_candle")) / total * 100, 1) if total else 0
    idm_rate       = round(sum(1 for t in all_trades if t.get("confluence_signals", {}).get("idm")) / total * 100, 1) if total else 0
    wave_rate      = round(sum(1 for t in all_trades if t.get("confluence_signals", {}).get("wave")) / total * 100, 1) if total else 0

    # V3.9: Average confluence score
    confluence_avg = round(sum(t.get("confluence_score", 0) for t in all_trades) / total, 2) if total else 0

    # V3.9: Regime distribution
    regime_counts = {}
    for t in all_trades:
        r = t.get("regime_state", "Unknown")
        regime_counts[r] = regime_counts.get(r, 0) + 1
    regime_distribution = {r: round(c / total * 100, 1) for r, c in regime_counts.items()} if total else {}

    # V3.9: Average position size
    avg_position_size = round(sum(t.get("position_size_pct", 100) for t in all_trades) / total, 1) if total else 0

    # Consecutive streaks
    max_cw = max_cl = cur_w = cur_l = 0
    for tr in all_trades:
        if tr["outcome"] == "WIN":
            cur_w += 1; cur_l = 0
        else:
            cur_l += 1; cur_w = 0
        max_cw = max(max_cw, cur_w)
        max_cl = max(max_cl, cur_l)

    # Grade breakdown (A/B/C/D from confluence thresholds)
    grade_stats = {}
    for g in ("A", "B", "C", "D"):
        g_trades = [t for t in all_trades if t.get("grade") == g]
        g_wins   = [t for t in g_trades  if t["outcome"] == "WIN"]
        g_total  = len(g_trades)
        g_pnl_r  = sum(t["pnl_r"] for t in g_trades)
        g_conf   = sum(t.get("confluence_score", 0) for t in g_trades) / g_total if g_total else 0
        grade_stats[g] = {
            "trades":       g_total,
            "wins":         len(g_wins),
            "losses":       g_total - len(g_wins),
            "win_rate":     round(len(g_wins) / g_total * 100, 1) if g_total else 0.0,
            "total_r":      round(g_pnl_r, 2),
            "avg_r":        round(g_pnl_r / g_total, 3) if g_total else 0.0,
            "avg_confluence": round(g_conf, 2),
        }

    # Monthly P&L
    monthly = {}
    for tr in all_trades:
        month = tr["entry_date"][:7]
        monthly[month] = round(monthly.get(month, 0.0) + tr["pnl_r"], 3)

    # Per-ticker breakdown
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
        # V3.9 metrics
        "confluence_avg":        confluence_avg,
        "regime_distribution":   regime_distribution,
        "avg_position_size_pct": avg_position_size,
        "wave_rate":             wave_rate,
        "wyckoff_rate":          wyckoff_rate,
        "rejection_rate":        rejection_rate,
        "idm_rate":              idm_rate,
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
            "confluence_gate":  CONFLUENCE_GATE,
            "regime_window":    REGIME_WINDOW,
            "sl_factor":        SL_FACTOR,
            "max_hold":         MAX_HOLD,
            "window":           WINDOW,
            "wyckoff_window":   WYCKOFF_WINDOW,
            "step":             STEP,
            "poi_band":         POI_BAND,
            "htf_window":       DAILY_HTF_WINDOW,
            "universe":         len(tickers),
            "ihsg_filter":      f"IHSG daily score >= {IHSG_MIN_SCORE}",
            "period":           BT_PERIOD,
            "version":          "v3.9 — Markov Regime + Elliott Wave + Confluence Gate + Risk-Scaled Sizing",
            "regime_config":    {r: dict(v) for r, v in REGIME_CONFIG.items()},
            "entry_filters":    (
                "Regime gate (Markov 7-state: skip Bear/Crisis/RangingVol) + "
                "8 hard filters: IHSG daily ≥50 + IHSG 40d bull + SMC Bullish "
                "+ Stock 40d trend up + 4H POI ±7% + HTF Bullish + Dual MA20 + Vol ≥1.1× | "
                "Confluence gate: ≥0.70 (7/10 signals) | "
                "Grade: A≥0.80 | B≥0.60 | C≥0.40 | D<0.40"
            ),
            "confluence_signals": (
                "1.Regime 2.IHSG40d 3.Stock40d 4.BOS 5.Wyckoff "
                "6.ElliottWave 7.IDM 8.Rejection 9.MA50 10.4HTrend"
            ),
            "tp_note":          (
                "Bull Quiet: 1.0/2.5/4.0R | "
                "Bull Volatile: 0.8/2.0/3.5R | "
                "Ranging: 0.6/1.5/2.5R"
            ),
            "sizing_note":      "position_size = confluence × regime_pos (capped at 1.0)",
            "trade_count_note": f"{total} trades over {BT_PERIOD}" + (
                " ⚠ below 100-trade minimum — verify regime + confluence filters"
                if total < 100 else ""
            ),
        },
    }

    _save_bt_cache(result)
    _cache_set("backtest_result", result)
    return result
