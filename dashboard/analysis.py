"""
analysis.py — All financial logic for the IDX Screener:
  - IHSG bull/bear scoring
  - Yahoo Finance OHLCV fetching & 4H resampling
  - GoAPI broker summary fetching (with persistent 24h cache + daily quota tracker)
  - Follow-the-Giant broker classification
  - SMC (Smart Money Concept) swing structure detection
  - Accumulation score calculation
  - FCA / suspended stock exclusion
  - v2 strategy enhancements: ATR SL/TP, weekly alignment, relative strength,
    retail contamination score, position sizing, session time filter, composite score
"""

import json
import logging
import numpy as np
import pandas as pd
import yfinance as yf
import requests
import random
import time
import datetime

# Suppress yfinance WARNING-level "possibly delisted" false positives that appear
# on large IDX batch downloads due to Yahoo Finance throttling.
# _download_chunked() already prevents the underlying throttle; this silences
# any residual noise. Genuine errors (level ERROR) are still surfaced.
logging.getLogger("yfinance").setLevel(logging.ERROR)
from functools import lru_cache
from pathlib import Path

import os
from dotenv import load_dotenv

load_dotenv()

# ── API Key ──────────────────────────────────────────────────────────────────
GOAPI_KEY  = os.getenv("GOAPI_KEY")
# IDX stock endpoints live at the root — no /v1 prefix
GOAPI_BASE = "https://api.goapi.io"

# ── Quota & Cache Config ──────────────────────────────────────────────────────
DAILY_API_LIMIT   = 28          # stay 2 under the 30/day hard limit
BROKER_CACHE_TTL  = 86400       # 24 hours — broker data is daily
OHLCV_CACHE_TTL   = 3600        # 1 hour for price data

BASE_DIR          = Path(__file__).resolve().parent.parent
BROKER_CACHE_FILE = BASE_DIR / "broker_cache.json"
API_USAGE_FILE    = BASE_DIR / "api_usage.json"

# ── Stock Universe ────────────────────────────────────────────────────────────
LQ45 = [
    "ACES", "ADRO", "AKRA", "AMRT", "ANTM", "ARTO", "ASII", "BBCA",
    "BBNI", "BBRI", "BBTN", "BMRI", "BRIS", "BRPT", "BUKA", "CPIN",
    "EMTK", "ESSA", "EXCL", "GOTO", "HRUM", "ICBP", "INCO", "INDF",
    "INKP", "INTP", "ITMG", "KLBF", "MDKA", "MEDC", "PGAS", "PTBA",
    "SIDO", "SMGR", "SRTG", "TBIG", "TINS", "TLKM", "TOWR", "UNTR", "UNVR",
]

# IDX80 additions and popular liquid IDX stocks not in LQ45
IDX_ADDITIONAL = [
    # Banking / Finance
    "BJBR", "BJTM", "BNGA", "BNLI", "BTPS", "NISP", "BFIN",
    # Consumer / Retail
    "HMSP", "MLBI", "ULTJ", "MAPI", "ERAA", "CLEO", "CMRY",
    # Healthcare
    "HEAL", "MIKA",
    # Property
    "BSDE", "PWON", "DMAS", "KIJA", "PANI", "SMRA",
    # Agribusiness / Energy
    "AALI", "DSNG", "ELSA", "PGEO", "TBLA", "TKIM",
    # Infrastructure / Transportation
    "JSMR", "SMDR", "ASSA",
    # Telco / Media
    "ISAT", "MNCN", "SCMA", "DNET",
    # Industrials / Manufacturing
    "AUTO", "SMSM", "JPFA", "ABMM", "BISI",
    # New Economy / EV Metals
    "AMMN", "MBMA", "NCKL",
    # Entertainment
    "FILM",
]

# Full universe: ~87 liquid IDX stocks
IDX_UNIVERSE = LQ45 + [t for t in IDX_ADDITIONAL if t not in LQ45]

# ── Broker Lists ─────────────────────────────────────────────────────────────
ALL_FOREIGN = ['YU','CG','KZ','CS','DP','GW','BK','DU','HD','AG','BQ',
               'RX','ZP','MS','XA','RB','TP','LS','DR','LH','AH','LG',
               'AK','AI','FS']

TIER1_FOREIGN = ['AK', 'BK', 'KZ']          # Strongest signal

LOCAL_BUMN   = ['CC', 'NI', 'OD', 'DX']     # Govt/pension money
RETAIL_FLAGS = ['YP', 'XC', 'XL', 'MG', 'AZ', 'KK']  # Retail / fast-trade

ALL_LOCAL = ['XC','PP','YO','ID','SH','BZ','AQ','AR','GA','SA','RF','ZR',
             'KI','PF','II','TX','TS','ES','MK','BS','AO','EL','PC','FO',
             'AF','HP','SC','IU','PD','IP','BF','IT','IN','YB','KS','YJ',
             'XL','GI','DD','DM','CD','MU','EP','OK','RO','IH','AP','PG',
             'GR','PS','AT','PO','RG','IF','MG','DH','AZ','SS','SF','BR',
             'TF','CP','BB','MI','AN','FZ','RS','AD','PI','QA','ZZ','JB']

# ── In-Memory Cache (OHLCV / IHSG) ───────────────────────────────────────────
_cache: dict = {}

def _cache_get(key, ttl=OHLCV_CACHE_TTL):
    entry = _cache.get(key)
    if entry and (time.time() - entry[0]) < ttl:
        return entry[1]
    return None

def _cache_set(key, data):
    _cache[key] = (time.time(), data)

# ── Persistent Broker Cache (disk, 24h TTL) ───────────────────────────────────
def _load_broker_disk() -> dict:
    if BROKER_CACHE_FILE.exists():
        try:
            with open(BROKER_CACHE_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def _save_broker_disk(cache: dict):
    try:
        with open(BROKER_CACHE_FILE, "w") as f:
            json.dump(cache, f)
    except Exception:
        pass

def _broker_cache_valid(entry: dict) -> bool:
    """True if cache entry is from today (calendar-day freshness).
    Falls back to rolling 24h check for legacy entries without a 'date' key."""
    today = datetime.date.today().isoformat()
    if entry.get("date") == today:
        return True
    # Legacy fallback: accept if within 24h (old entries without 'date' key)
    return (time.time() - entry.get("ts", 0)) < BROKER_CACHE_TTL

def _get_broker_from_disk(ticker: str) -> dict | None:
    cache = _load_broker_disk()
    entry = cache.get(ticker)
    if entry and _broker_cache_valid(entry):
        return entry["data"]
    return None

def _put_broker_to_disk(ticker: str, data: dict):
    cache = _load_broker_disk()
    cache[ticker] = {
        "ts":   time.time(),
        "date": datetime.date.today().isoformat(),  # calendar-day key
        "data": data,
    }
    _save_broker_disk(cache)

# ── Daily API Usage Counter ───────────────────────────────────────────────────
def _get_api_usage() -> dict:
    today = datetime.date.today().isoformat()
    if API_USAGE_FILE.exists():
        try:
            with open(API_USAGE_FILE, "r") as f:
                usage = json.load(f)
            if usage.get("date") == today:
                return usage
        except Exception:
            pass
    return {"date": today, "count": 0, "tickers": []}

def _save_api_usage(usage: dict):
    try:
        with open(API_USAGE_FILE, "w") as f:
            json.dump(usage, f)
    except Exception:
        pass

def _increment_api_usage(ticker: str) -> int:
    usage = _get_api_usage()
    usage["count"] += 1
    if ticker not in usage.get("tickers", []):
        usage.setdefault("tickers", []).append(ticker)
    _save_api_usage(usage)
    return usage["count"]

def api_remaining() -> int:
    return max(0, DAILY_API_LIMIT - _get_api_usage()["count"])

def get_api_status() -> dict:
    usage = _get_api_usage()
    disk  = _load_broker_disk()
    now   = time.time()
    cached_tickers = [t for t, v in disk.items() if _broker_cache_valid(v)]
    return {
        "date":              usage["date"],
        "calls_today":       usage["count"],
        "calls_remaining":   api_remaining(),
        "daily_limit":       DAILY_API_LIMIT,
        "tickers_called":    usage.get("tickers", []),
        "broker_cached":     cached_tickers,
        "broker_cache_size": len(cached_tickers),
    }

# ── FCA / Suspended Exclusion ─────────────────────────────────────────────────
_KNOWN_FCA_FALLBACK: set[str] = {
    "ATAP", "RONY", "TELE", "JSKY", "IATA", "BPTR", "FUJI", "MYOH",
    "SMKL", "FIRE", "NELY", "TAXI", "SUGI", "MITI", "DIGI",
}

def fetch_fca_suspended_stocks() -> set:
    cached = _cache_get("fca_stocks", ttl=21600)
    if cached is not None:
        return set(cached)
    endpoints = [
        "https://www.idx.co.id/umbraco/Surface/ListedCompany/GetStockOnSpecialMonitoring",
        "https://www.idx.co.id/primary/ListedCompany/GetStockOnSpecialMonitoring",
    ]
    headers = {"User-Agent": "Mozilla/5.0"}
    for url in endpoints:
        try:
            r = requests.get(url, headers=headers, timeout=10)
            if r.status_code == 200:
                data = r.json()
                rows = data.get("data", data.get("Data", []))
                tickers = {
                    (row.get("StockCode") or row.get("Kode") or "").upper()
                    for row in rows
                    if row.get("StockCode") or row.get("Kode")
                }
                if tickers:
                    _cache_set("fca_stocks", list(tickers))
                    return tickers
        except Exception:
            pass
    _cache_set("fca_stocks", list(_KNOWN_FCA_FALLBACK))
    return _KNOWN_FCA_FALLBACK.copy()

# ── IHSG Bull/Bear Score ─────────────────────────────────────────────────────
def fetch_ihsg():
    cached = _cache_get("ihsg")
    if cached:
        return cached
    try:
        # IHSG Ticker is ^JKSE
        df = yf.download("^JKSE", period="1y", interval="1d", progress=False, auto_adjust=True)
        if df.empty:
            return {"score": 0, "status": "No Data", "current": 0, "ma20": 0, "ma50": 0, "ma200": 0}

        # Handle multi-index columns if they exist
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        
        # Ensure we have a Close column
        if 'Close' not in df.columns:
            return {"score": 0, "status": "Error: No Close Data", "current": 0, "ma20": 0, "ma50": 0, "ma200": 0}

        close = df['Close'].dropna()
        if len(close) < 200:
            # Not enough for MA200, maybe it's a new ticker or limited history
            ma200_val = close.mean() 
        else:
            ma200_val = close.rolling(200).mean().iloc[-1]

        current = float(close.iloc[-1])
        ma20   = float(close.rolling(20).mean().iloc[-1])
        ma50   = float(close.rolling(50).mean().iloc[-1])
        ma200  = float(ma200_val)

        score = 0
        if current > ma20:  score += 30
        if current > ma50:  score += 30
        if current > ma200: score += 40

        if score >= 70:   status = "Strong Bull 🟢"
        elif score >= 50: status = "Bull 🟡"
        elif score >= 30: status = "Bear 🔴"
        else:             status = "Strong Bear 💀"

        result = {
            "score": int(score), "status": status,
            "current": round(current, 2),
            "ma20": round(ma20, 2), "ma50": round(ma50, 2), "ma200": round(ma200, 2)
        }
        _cache_set("ihsg", result)
        return result
    except Exception as e:
        print(f"IHSG fetch error: {e}")
        return {"score": 0, "status": "Error Loading Data", "current": 0, "ma20": 0, "ma50": 0, "ma200": 0, "error": str(e)}

# ── Yahoo Finance OHLCV ───────────────────────────────────────────────────────
def _df_to_records(df: pd.DataFrame) -> list:
    """Convert OHLCV DataFrame to list of dicts for JSON serialization."""
    if df.empty:
        return []
    df = df.copy()
    df.index = df.index.astype(str)
    df = df.where(pd.notnull(df), None)
    return df.reset_index().rename(columns={"index": "Date"}).to_dict(orient="records")

def fetch_ohlcv(ticker: str):
    """Returns dict with keys: data_1d, data_4h, data_1h — each a list of OHLCV dicts."""
    cache_key = f"ohlcv_{ticker}"
    cached = _cache_get(cache_key)
    if cached:
        return cached

    result = {"data_1d": [], "data_4h": [], "data_1h": [], "error": None}
    try:
        raw_1d = yf.download(ticker, period="6mo", interval="1d", progress=False, auto_adjust=True)
        raw_1h = yf.download(ticker, period="1mo", interval="1h", progress=False, auto_adjust=True)

        for df in [raw_1d, raw_1h]:
            df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]

        raw_4h = raw_1h.resample("4h").agg({
            "Open": "first", "High": "max", "Low": "min",
            "Close": "last", "Volume": "sum"
        }).dropna()

        result["data_1d"] = _df_to_records(raw_1d[["Open","High","Low","Close","Volume"]])
        result["data_4h"] = _df_to_records(raw_4h)
        result["data_1h"] = _df_to_records(raw_1h[["Open","High","Low","Close","Volume"]])

        _cache_set(cache_key, result)
    except Exception as e:
        result["error"] = str(e)
    return result

# ── GoAPI Broker Summary ─────────────────────────────────────────────────────
def _parse_goapi_response(data: dict) -> tuple:
    """
    GoAPI returns broker summary data in several possible shapes.
    Normalises all of them to (buy_list, sell_list) where each entry is
    {"broker": str, "vol": int, "val": int, "avg": float}.

    Handled formats
    ---------------
    A) {"data": {"results": [{code, side, lot, value, avg}, ...]}}   ← old parser only knew this
    B) {"data": [{code, side, lot, value, avg}, ...]}                 ← data IS the list
    C) {"data": {"buy": [{...}], "sell": [{...}]}}                    ← pre-split by side
    D) {"data": {"broker_summary": [{...}]}}                          ← named key variant

    Returns (buy_list, sell_list) — empty lists if no data found.
    """
    buy_list, sell_list = [], []

    inner = data.get("data", {})

    # Format C: pre-split buy/sell
    if isinstance(inner, dict) and ("buy" in inner or "sell" in inner):
        def norm(entries):
            out = []
            for row in (entries or []):
                code = (row.get("code") or row.get("broker_code") or
                        (row.get("broker") or {}).get("code") or "??")
                out.append({
                    "broker": str(code).upper(),
                    "vol":    int(row.get("lot", row.get("volume", 0)) or 0),
                    "val":    int(row.get("value", 0) or 0),
                    "avg":    float(row.get("avg", row.get("avg_price", 0)) or 0),
                })
            return out
        return norm(inner.get("buy", [])), norm(inner.get("sell", []))

    # Flatten to a list of rows
    if isinstance(inner, list):
        rows = inner
    elif isinstance(inner, dict):
        # Format A: results key
        rows = inner.get("results",
               inner.get("broker_summary",
               inner.get("data", [])))
        if not isinstance(rows, list):
            rows = []
    else:
        rows = []

    # Parse the flat list (each row has a "side" field)
    for row in rows:
        code = (row.get("code") or row.get("broker_code") or
                (row.get("broker") or {}).get("code") or "??")
        entry = {
            "broker": str(code).upper(),
            "vol":    int(row.get("lot", row.get("volume", 0)) or 0),
            "val":    int(row.get("value", 0) or 0),
            "avg":    float(row.get("avg", row.get("avg_price", 0)) or 0),
        }
        side = str(row.get("side", "")).upper()
        if side in ("BUY", "B"):
            buy_list.append(entry)
        elif side in ("SELL", "S"):
            sell_list.append(entry)

    return buy_list, sell_list


def _try_fetch_date(url: str, headers: dict, date_str: str) -> tuple:
    """Single GoAPI request for one date.  Returns (buy_list, sell_list, raw_response_snippet)."""
    r = requests.get(f"{url}?date={date_str}", headers=headers, timeout=10)
    if r.status_code != 200:
        raise ValueError(f"HTTP {r.status_code}: {r.text[:200]}")
    data = r.json()
    buy_list, sell_list = _parse_goapi_response(data)
    return buy_list, sell_list, data


def fetch_broker_summary(ticker: str):
    """
    Fetch broker buy/sell data.  Priority:
      1. In-memory cache (1h)
      2. Persistent disk cache (24h)
      3. Live GoAPI call (if daily quota allows)
         - tries today, then yesterday if today is empty (weekend / holiday)
      4. Simulated fallback (in-memory only — never persisted to disk)

    The source field tells you exactly what happened:
      "live"      — fresh GoAPI call, data persisted to 24h disk cache
      "cached"    — served from disk cache (no API call made)
      "simulated" — GoAPI call failed or quota gone; data is synthetic
    """
    # 1. In-memory cache
    mem_key = f"broker_{ticker}"
    cached = _cache_get(mem_key)
    if cached:
        return cached

    # 2. Persistent disk cache (24h)
    disk_data = _get_broker_from_disk(ticker)
    if disk_data:
        _cache_set(mem_key, disk_data)
        return disk_data

    # 3. Live GoAPI call — only if quota available and key is set
    if not GOAPI_KEY:
        sim = _simulate_broker_data(ticker)
        sim["error"] = "GOAPI_KEY not set in .env — check your environment variables."
        sim["source"] = "simulated"
        _cache_set(mem_key, sim)
        return sim

    if api_remaining() <= 0:
        sim = _simulate_broker_data(ticker)
        sim["error"] = "Daily GoAPI quota exhausted (28/day). Showing simulated data."
        sim["quota_exhausted"] = True
        _cache_set(mem_key, sim)
        return sim

    url     = f"{GOAPI_BASE}/stock/idx/{ticker}/broker_summary"
    headers = {"X-API-KEY": GOAPI_KEY}
    today     = datetime.date.today().strftime('%Y-%m-%d')
    yesterday = (datetime.date.today() - datetime.timedelta(days=1)).strftime('%Y-%m-%d')

    last_error = None
    for date_str in (today, yesterday):
        try:
            buy_list, sell_list, _ = _try_fetch_date(url, headers, date_str)
            if buy_list or sell_list:
                buy_list.sort(key=lambda x: x["vol"],  reverse=True)
                sell_list.sort(key=lambda x: x["vol"], reverse=True)
                total_vol = sum(b["vol"] for b in buy_list) + sum(s["vol"] for s in sell_list)

                formatted = {
                    "source":          "live",
                    "data_date":       date_str,
                    "data":            {"buy": buy_list, "sell": sell_list, "total_vol": total_vol},
                    "error":           None,
                    "quota_exhausted": False,
                }
                _increment_api_usage(ticker)
                _cache_set(mem_key, formatted)
                _put_broker_to_disk(ticker, formatted)
                return formatted
            # Got 200 but empty data (market closed, no trades) — try previous day
        except Exception as exc:
            last_error = str(exc)
            break   # Hard error (auth / network) — no point trying yesterday

    # 4. Simulated fallback — in-memory only, never saved to disk
    sim = _simulate_broker_data(ticker)
    sim["error"] = (
        f"GoAPI returned no usable data for {ticker}. "
        f"Last error: {last_error or 'empty response'}. "
        "Showing simulated data."
    )
    sim["source"] = "simulated"
    _cache_set(mem_key, sim)
    return sim

def _simulate_broker_data(ticker: str) -> dict:
    rng = random.Random(ticker + str(int(time.time() // 3600)))
    buy_brokers  = rng.sample(ALL_FOREIGN[:10] + LOCAL_BUMN + RETAIL_FLAGS, 8)
    sell_brokers = rng.sample(ALL_LOCAL[:10] + RETAIL_FLAGS, 6)

    total_vol = rng.randint(5_000_000, 50_000_000)

    def make_entries(brokers):
        entries = []
        for b in brokers:
            vol = rng.randint(100_000, int(total_vol * 0.3))
            val = vol * rng.randint(2000, 12000)
            entries.append({"broker": b.upper(), "vol": vol, "val": val, "avg": val // max(vol,1)})
        entries.sort(key=lambda x: x["vol"], reverse=True)
        return entries

    return {
        "source": "simulated",
        "data": {
            "buy":  make_entries(buy_brokers),
            "sell": make_entries(sell_brokers),
            "total_vol": total_vol,
        },
        "error": None
    }

# ── Follow-the-Giant Analysis ────────────────────────────────────────────────
def analyze_flow(broker_data: dict) -> dict:
    """
    Analyses broker buy data:
    - Classifies top-3 buyers
    - Checks for foreign/BUMN/retail dominance
    - Computes accumulation score
    - Returns signal, category, and trade eligibility
    """
    raw = broker_data.get("data", {})
    buy_list  = raw.get("buy", [])
    total_vol = raw.get("total_vol", sum(b.get("vol", 0) for b in buy_list))

    if not buy_list:
        return {"signal": "No Data", "eligible": False}

    top3 = buy_list[:3]
    top3_names = [b["broker"].upper() for b in top3]
    top3_vol   = sum(b.get("vol", 0) for b in top3)
    acc_score  = top3_vol / total_vol if total_vol > 0 else 0

    is_foreign  = [n in ALL_FOREIGN  for n in top3_names]
    is_bumn     = [n in LOCAL_BUMN   for n in top3_names]
    is_retail   = [n in RETAIL_FLAGS for n in top3_names]
    is_tier1    = [n in TIER1_FOREIGN for n in top3_names]

    foreign_count = sum(is_foreign)
    retail_count  = sum(is_retail)
    bumn_count    = sum(is_bumn)
    tier1_count   = sum(is_tier1)

    # Signal logic
    if tier1_count >= 2:
        signal   = "🚀 HIGHEST CONVICTION: Tier-1 Foreign Funds (AK/BK/KZ) Accumulating"
        category = "tier1_foreign"
        eligible = True
    elif foreign_count >= 2 and bumn_count >= 1:
        signal   = "⭐ STRONGEST SIGNAL: Foreign + Government Convergence (Positional)"
        category = "convergence"
        eligible = True
    elif foreign_count >= 2:
        signal   = "✅ FOREIGN FLOW DETECTED: Accumulation probable, watch for BOS"
        category = "foreign"
        eligible = True
    elif retail_count >= 2:
        signal   = "⚠️ CAUTION — Retail/Fast-Trade Dominated: Likely short-term noise"
        category = "retail"
        eligible = False
    elif bumn_count >= 1:
        signal   = "ℹ️ Government/Pension money present — moderate confidence"
        category = "bumn"
        eligible = True
    else:
        signal   = "😐 Mixed activity — No dominant smart-money signal"
        category = "neutral"
        eligible = False

    # Accumulation label
    if acc_score >= 0.5:
        acc_label = "🐋 Massive Accumulation (Whale at work!)"
    elif acc_score >= 0.3:
        acc_label = "📈 Accumulation Detected"
    else:
        acc_label = "Normal Activity"

    return {
        "top3": [{"broker": n, "vol": b.get("vol", 0), "val": b.get("val", 0)}
                 for n, b in zip(top3_names, top3)],
        "top3_names": top3_names,
        "acc_score": round(acc_score * 100, 1),
        "acc_label": acc_label,
        "signal": signal,
        "category": category,
        "eligible": eligible,
        "foreign_count": foreign_count,
        "bumn_count": bumn_count,
        "retail_count": retail_count,
    }

# ── SMC — Smart Money Concept ─────────────────────────────────────────────────
def extract_smc(records: list, timeframe: str = "1H") -> dict:
    """
    Detects BOS, CHoCH, IDM, Sweeps, and POI zone — faithful port of the
    LuxAlgo 'Market Structure with Inducements & Sweeps' PineScript indicator.

    Algorithm (matches LuxAlgo exactly):
      • Swing detection: high[len] > ta.highest(len) — forward-only, NO look-ahead
      • CHoCH: close crosses previous swing HIGH (→ Bullish) or LOW (→ Bearish)
      • IDM: shorter-period (len=3) swing is swept BEFORE BOS is allowed
      • BOS: close breaks trailing high/low AFTER IDM is confirmed
      • Sweeps: wick extends beyond structure but close reverses back inside

    LuxAlgo defaults: CHoCH period = 8, IDM period = 3.
    """
    if len(records) < 20:
        return {"bias": "Neutral", "error": "Not enough data"}

    # ── Data normalisation ────────────────────────────────────────────────
    if isinstance(records, pd.DataFrame):
        df = records.copy()
        if "Date" in df.columns:
            df = df.rename(columns={"Date": "date"})
        elif "Datetime" in df.columns:
            df = df.rename(columns={"Datetime": "date"})
        elif df.index.name in ("Date", "Datetime") or isinstance(df.index, pd.DatetimeIndex):
            df["date"] = df.index.astype(str)
        else:
            df["date"] = df.index.astype(str)
    else:
        df = pd.DataFrame(records)
        df = df.rename(columns={"Date": "date"})

    highs  = df["High"].astype(float).values
    lows   = df["Low"].astype(float).values
    closes = df["Close"].astype(float).values
    dates  = df["date"].astype(str).values
    n      = len(highs)

    # ── LuxAlgo parameters ────────────────────────────────────────────────
    LEN       = 8   # CHoCH detection period  (input default = 8)
    SHORT_LEN = 3   # IDM  detection period   (input default = 3)

    # ── Step 1: Forward-only swing detection (NO look-ahead) ──────────────
    # LuxAlgo: os := high[len] > ta.highest(len) ? 0 : low[len] < ta.lowest(len) ? 1 : os[1]
    # "high[len]" = the bar LEN bars ago; ta.highest(len) = max of the MOST RECENT len bars.
    # So a swing high at bar (i-LEN) is confirmed only after LEN subsequent bars are lower.
    def _swings(length):
        os = 0; prev_os = 0
        tops, btms = [], []
        for i in range(length, n):
            ref_hi = highs[i - length]
            ref_lo = lows [i - length]
            upper  = max(highs[i - length + 1 : i + 1])   # ta.highest(length)
            lower  = min(lows [i - length + 1 : i + 1])   # ta.lowest(length)

            prev_os = os
            if   ref_hi > upper: os = 0
            elif ref_lo < lower: os = 1

            if os == 0 and prev_os != 0:
                tops.append({"idx": i - length, "price": float(ref_hi),
                             "date": dates[i - length]})
            if os == 1 and prev_os != 1:
                btms.append({"idx": i - length, "price": float(ref_lo),
                             "date": dates[i - length]})
        return tops, btms

    main_tops,  main_btms  = _swings(LEN)
    short_tops, short_btms = _swings(SHORT_LEN)

    if not main_tops or not main_btms:
        return {"bias": "Neutral", "error": "Insufficient swing data",
                "swing_highs": [], "swing_lows": []}

    # ── Step 2: State machine ─────────────────────────────────────────────
    # Build index → price maps for O(1) lookup
    mt_map = {s["idx"]: s["price"] for s in main_tops}
    mb_map = {s["idx"]: s["price"] for s in main_btms}
    st_map = {s["idx"]: s["price"] for s in short_tops}
    sb_map = {s["idx"]: s["price"] for s in short_btms}

    os           = 0                       # structural direction (0 = os; 1 = bullish)
    topy         = main_tops[0]["price"]
    btmy         = main_btms[0]["price"]
    stopy        = short_tops[0]["price"] if short_tops else topy
    sbtmy        = short_btms[0]["price"] if short_btms else btmy

    top_crossed  = False; btm_crossed  = False
    stop_crossed = False; sbtm_crossed = False

    t_max   = float(highs[0]); t_max_x = 0   # trailing highest high since last CHoCH
    t_min   = float(lows[0]);  t_min_x = 0   # trailing lowest  low  since last CHoCH

    choch_events = []   # {"bar", "level", "type": "bullish"|"bearish"}
    bos_events   = []
    idm_events   = []
    sweep_events = []

    for i in range(n):
        c = float(closes[i]); h = float(highs[i]); l = float(lows[i])

        # Update main swings
        if i in mt_map: topy  = mt_map[i]; top_crossed  = False
        if i in mb_map: btmy  = mb_map[i]; btm_crossed  = False
        # Update short swings (IDM tracker)
        if i in st_map: stopy = st_map[i]
        if i in sb_map: sbtmy = sb_map[i]

        prev_os = os

        # ── CHoCH: close crosses previous structural swing ────────────────
        if c > topy and not top_crossed:
            os = 1; top_crossed = True
            choch_events.append({"bar": i, "level": topy, "type": "bullish"})
        if c < btmy and not btm_crossed:
            os = 0; btm_crossed = True
            choch_events.append({"bar": i, "level": btmy, "type": "bearish"})

        # Reset trailing extremes on structural flip
        if os != prev_os:
            t_max = h; t_max_x = i
            t_min = l; t_min_x = i
            stop_crossed = False; sbtm_crossed = False

        # ── Bullish structure (os == 1) ────────────────────────────────────
        if os == 1:
            # IDM: shorter swing low swept (liquidity grab before BOS)
            if l < sbtmy and not sbtm_crossed and abs(sbtmy - btmy) > 1e-9:
                idm_events.append({"bar": i, "level": sbtmy, "type": "bullish"})
                sbtm_crossed = True
            # BOS: close above trailing high, IDM must have been swept first
            if c > t_max and sbtm_crossed:
                bos_events.append({"bar": i, "level": t_max, "type": "bullish"})
                sbtm_crossed = False
            # Sweep: wick above trailing high but close rejected back below
            if h > t_max and c < t_max and i - t_max_x > 1:
                sweep_events.append({"bar": i, "level": t_max, "type": "bull_sweep"})

        # ── Bearish structure (os == 0) ────────────────────────────────────
        elif os == 0:
            # IDM: shorter swing high swept
            if h > stopy and not stop_crossed and abs(stopy - topy) > 1e-9:
                idm_events.append({"bar": i, "level": stopy, "type": "bearish"})
                stop_crossed = True
            # BOS: close below trailing low after IDM
            if c < t_min and stop_crossed:
                bos_events.append({"bar": i, "level": t_min, "type": "bearish"})
                stop_crossed = False
            # Sweep: wick below trailing low but close recovers
            if l < t_min and c > t_min and i - t_min_x > 1:
                sweep_events.append({"bar": i, "level": t_min, "type": "bear_sweep"})

        # Update trailing extremes
        if h > t_max: t_max = h; t_max_x = i
        if l < t_min: t_min = l; t_min_x = i

    # ── Step 3: Bias and POI zone ─────────────────────────────────────────
    # os == 1 after last CHoCH → Bullish; os == 0 with a prior CHoCH → Bearish
    if   os == 1:                  bias = "Bullish"
    elif choch_events:             bias = "Bearish"
    else:                          bias = "Neutral"

    current_price = float(closes[-1])

    if bias == "Bullish":
        # Demand zone: around last confirmed swing LOW (where buy orders sit)
        poi_low   = round(btmy, 2)
        poi_high  = round(btmy * 1.02, 2)        # 2% band above swing low
        bos_lv    = bos_events[-1]["level"]   if bos_events   else t_max
        choch_lv  = choch_events[-1]["level"] if choch_events else topy
        idm_lv    = idm_events[-1]["level"]   if idm_events   else btmy
        entry     = round(poi_high, 2)
        sl        = round(poi_low * 0.985, 2)
        tp        = round(entry + (entry - sl) * 1.8, 2)

    elif bias == "Bearish":
        # Supply zone: around last confirmed swing HIGH
        poi_low   = round(topy * 0.98, 2)
        poi_high  = round(topy, 2)
        bos_lv    = bos_events[-1]["level"]   if bos_events   else t_min
        choch_lv  = choch_events[-1]["level"] if choch_events else btmy
        idm_lv    = idm_events[-1]["level"]   if idm_events   else topy
        entry     = round(poi_low, 2)
        sl        = round(poi_high * 1.015, 2)
        tp        = round(entry - (sl - entry) * 1.8, 2)

    else:
        poi_low = poi_high = bos_lv = choch_lv = idm_lv = 0.0
        entry = sl = tp = 0.0

    rr = round(abs(tp - entry) / max(abs(entry - sl), 0.0001), 2)

    return {
        "timeframe":     timeframe,
        "bias":          bias,
        "bos":           round(float(bos_lv),   2),
        "choch":         round(float(choch_lv), 2),
        "poi_low":       round(float(poi_low),  2),
        "poi_high":      round(float(poi_high), 2),
        "idm":           round(float(idm_lv),   2),
        "eqh":           round(float(topy),     2),
        "eql":           round(float(btmy),     2),
        "entry":         entry,
        "sl":            sl,
        "tp":            tp,
        "rr":            rr,
        "current":       round(current_price, 2),
        "swing_highs":   main_tops[-5:],
        "swing_lows":    main_btms[-5:],
        "choch_events":  choch_events[-3:],
        "bos_events":    bos_events[-3:],
        "idm_events":    idm_events[-3:],
        "sweep_events":  sweep_events[-3:],
        "error":         None,
    }

# ── Trend + Volume Validation ─────────────────────────────────────────────────
def validate_trend_volume(records_1d: list) -> dict:
    if len(records_1d) < 20:
        return {"trend_valid": False, "vol_valid": False}
    if isinstance(records_1d, pd.DataFrame):
        df = records_1d
    else:
        df = pd.DataFrame(records_1d)
    close = df["Close"].astype(float)
    vol   = df["Volume"].astype(float)
    ma20  = close.rolling(20).mean().iloc[-1]
    avg10v = vol.rolling(10).mean().iloc[-1]
    curr_close = float(close.iloc[-1])
    curr_vol   = float(vol.iloc[-1])
    return {
        "trend_valid": bool(curr_close > ma20),
        "vol_valid":   bool(curr_vol > 1.5 * avg10v),
        "current_price": round(curr_close, 2),
        "ma20": round(float(ma20), 2),
        "current_vol": int(curr_vol),
        "avg_vol_10d": int(avg10v),
    }

# ── Rejection Candle Helper (shared by screener + backtest) ──────────────────
def _is_rejection_candle(opens, closes, highs, lows, idx: int) -> bool:
    """
    True if the bar at idx shows buyers defending a POI zone.
    Condition A: green candle (close > open).
    Condition B: relaxed hammer — lower wick >= 1× body AND lower wick > upper wick.
    Used by both screen_market() (on 1H bars) and backtest (on daily bars).
    """
    if idx < 0 or idx >= len(closes):
        return False
    o = float(opens[idx]);  c = float(closes[idx])
    h = float(highs[idx]);  l = float(lows[idx])
    if c > o:
        return True
    body       = abs(c - o)
    lower_wick = min(o, c) - l
    upper_wick = h - max(o, c)
    if body > 0 and lower_wick >= body and lower_wick > upper_wick:
        return True
    return False


# ── Chunked batch downloader ─────────────────────────────────────────────────
# yfinance can return false "possibly delisted" warnings when downloading
# 80+ IDX tickers in a single request due to Yahoo Finance throttling.
# Splitting into chunks of 20 keeps each request small enough to succeed.
_DOWNLOAD_CHUNK = 20

def _download_chunked(tickers_jk: list, period: str, interval: str) -> pd.DataFrame:
    """
    Downloads OHLCV for *tickers_jk* in chunks of _DOWNLOAD_CHUNK to prevent
    the Yahoo Finance throttle that produces false 'possibly delisted' warnings
    on large IDX batch requests.

    Returns a MultiIndex (ticker → OHLCV column) DataFrame identical to what
    yf.download(..., group_by='ticker') produces.
    """
    frames = []
    for i in range(0, len(tickers_jk), _DOWNLOAD_CHUNK):
        chunk = tickers_jk[i : i + _DOWNLOAD_CHUNK]
        try:
            df = yf.download(
                " ".join(chunk), period=period, interval=interval,
                group_by="ticker", progress=False, auto_adjust=True,
            )
            if df.empty:
                continue
            # Single-ticker download has flat columns — wrap in MultiIndex
            if not isinstance(df.columns, pd.MultiIndex) and len(chunk) == 1:
                df.columns = pd.MultiIndex.from_tuples(
                    [(chunk[0], c) for c in df.columns]
                )
            frames.append(df)
        except Exception:
            pass   # chunk failed; affected tickers will be skipped downstream

    if not frames:
        return pd.DataFrame()
    result = pd.concat(frames, axis=1)
    # Drop any accidental duplicate columns
    return result.loc[:, ~result.columns.duplicated()]


# ── Market Screener ──────────────────────────────────────────────────────────
def screen_market():
    """
    Screens all IDX_UNIVERSE stocks (minus FCA/suspended):
      1. Batch-downloads OHLCV from Yahoo Finance (free, unlimited)
      2. Applies technical + SMC filters — all passing stocks become candidates
      3. For each candidate:
           - Uses 24h persistent broker cache if available (no API cost)
           - Calls GoAPI only if not cached AND daily quota allows (max 28/day)
           - Computes v2 enhancements: weekly alignment, relative strength,
             flow quality, ATR levels, and composite score
      4. Returns ALL technical candidates sorted by composite score
    """
    cache_key = "market_screener"
    cached = _cache_get(cache_key, ttl=1800)
    if cached:
        return cached

    # ── IHSG daily gate — checked once, applied in routing block ────────────────
    ihsg_daily    = fetch_ihsg()
    ihsg_daily_ok = ihsg_daily.get("score", 0) >= 50   # Bull or Strong Bull only

    # Exclude FCA / suspended stocks
    fca_set = fetch_fca_suspended_stocks()
    universe = [t for t in IDX_UNIVERSE if t not in fca_set]

    tickers = [f"{t}.JK" for t in universe]

    try:
        data_1d = _download_chunked(tickers, period="3mo", interval="1d")
        data_1h = _download_chunked(tickers, period="1mo", interval="1h")
    except Exception as e:
        return {"error": f"Batch download failed: {str(e)}"}

    technical_candidates = []

    for t in universe:
        t_jk = f"{t}.JK"
        try:
            if isinstance(data_1d.columns, pd.MultiIndex):
                if t_jk not in data_1d.columns.get_level_values(0):
                    continue
                df_1d = data_1d[t_jk].dropna()
                df_1h = data_1h[t_jk].dropna()
            else:
                df_1d = data_1d.dropna()
                df_1h = data_1h.dropna()
        except Exception:
            continue

        if len(df_1d) < 20 or len(df_1h) < 20:
            continue

        # ── Daily trend check (MA20 — structural direction) ───────────────────
        tv = validate_trend_volume(df_1d)
        if not tv["trend_valid"]:
            continue   # price below daily MA20 → downtrend, skip

        # ── 1H SMC bias (BOS/CHoCH on hourly structure) ──────────────────────
        # 1H is used for DIRECTION only — not for POI proximity.
        smc_1h = extract_smc(df_1h, "1H")
        if smc_1h.get("bias") != "Bullish":
            continue

        # ── 4H SMC: trend bias + POI (resample from 1H — no extra request) ───
        smc_4h_bonus = {}
        df_4h        = pd.DataFrame()
        try:
            df_4h = df_1h.resample("4h").agg({
                "Open": "first", "High": "max", "Low": "min",
                "Close": "last", "Volume": "sum",
            }).dropna()
            if len(df_4h) >= 10:
                smc_4h_bonus = extract_smc(df_4h, "4H")
        except Exception:
            pass
        trend_4h_bullish = smc_4h_bonus.get("bias") == "Bullish"

        # ── 4H MA20 trend confirmation ────────────────────────────────────────
        trend_4h_ma20 = False
        if len(df_4h) >= 20:
            close_4h      = df_4h["Close"].astype(float)
            ma20_4h       = float(close_4h.rolling(20).mean().iloc[-1])
            trend_4h_ma20 = bool(float(close_4h.iloc[-1]) > ma20_4h)

        # Dual MA20 hard gate: price must be above MA20 on BOTH daily AND 4H
        if not trend_4h_ma20:
            continue   # 4H shows downtrend — skip

        # ── 1H price + 4H POI (4H is the ONLY demand zone reference) ─────────
        curr_price_1h = float(df_1h["Close"].iloc[-1])
        poi_low_4h    = smc_4h_bonus.get("poi_low",  0)
        poi_high_4h   = smc_4h_bonus.get("poi_high", 0)

        def _near_poi(price, lo, hi, band=0.05):
            return lo > 0 and hi > 0 and price <= hi * (1 + band) and price >= lo * (1 - band)

        near_poi_4h = _near_poi(curr_price_1h, poi_low_4h, poi_high_4h)

        # Entry gate: must be near 4H demand zone
        if not near_poi_4h:
            continue

        # ── Rejection candle on the most recent 1H bar ────────────────────────
        opens_1h  = df_1h["Open"].values.astype(float)
        closes_1h = df_1h["Close"].values.astype(float)
        highs_1h  = df_1h["High"].values.astype(float)
        lows_1h   = df_1h["Low"].values.astype(float)
        rejection_1h = _is_rejection_candle(opens_1h, closes_1h, highs_1h, lows_1h, len(df_1h) - 1)
        if not rejection_1h:
            continue

        # ── Volume confirmation on 1H bars (10-period rolling average) ────────
        vol_1h   = df_1h["Volume"].values.astype(float)
        avg10_1h = float(pd.Series(vol_1h).rolling(10).mean().iloc[-1])
        vol_1h_valid = avg10_1h > 0 and bool(vol_1h[-1] > 1.1 * avg10_1h)
        if not vol_1h_valid:
            continue

        # ── Flags ─────────────────────────────────────────────────────────────
        # All hard-gate conditions are guaranteed True at this point
        flags = ["Near 4H POI", "Rejection 1H", "Vol 1H", "Daily > MA20", "4H > MA20"]
        if trend_4h_bullish: flags.append("4H Bullish")

        # ── Tech score ────────────────────────────────────────────────────────
        # 4H POI (40) + trend (30) are guaranteed; volume + 4H bias are variable
        tech_score = (
            40                                   # 4H POI — hard gate passed
          + (30 if vol_1h_valid    else 0)
          + 30                                   # dual MA20 trend — hard gate passed
          + (15 if trend_4h_bullish else 0)      # 4H SMC bias bonus
        )

        technical_candidates.append({
            "ticker":     t,
            "score":      tech_score,
            "tech_score": tech_score,
            "price":      curr_price_1h,
            "flags":      flags,
            "smc":        smc_1h,
            "smc_4h":     smc_4h_bonus,
            "poi_4h":     {"low": poi_low_4h, "high": poi_high_4h},
            "trend_vol":  tv,
            "df_1d":      df_1d,  # keep for ATR computation (not serialised)
        })

    technical_candidates.sort(key=lambda x: x["score"], reverse=True)
    passed_technical_set = {c["ticker"] for c in technical_candidates}

    # ── Load broker disk cache once (shared by enrichment + watchlist) ────────
    disk_cache = _load_broker_disk()
    now        = time.time()

    # ── Enrich technical candidates with v2 + broker flow ────────────────────
    # Signals are split into three buckets:
    #   confirmed  — flow eligible (smart money confirmed) ← TRUE signals
    #   watch      — no broker data yet (quota/no cache)   ← need verification
    #   caution    — broker data available but flow is NOT eligible (retail)
    confirmed  = []
    watch      = []
    caution    = []

    for cand in technical_candidates:
        t     = cand["ticker"]
        t_jk  = f"{t}.JK"
        df_1d = cand.pop("df_1d", None)  # remove DataFrame before serialisation

        # ── Broker flow: cache → live GoAPI → pending ─────────────────────────
        has_cache = t in disk_cache and _broker_cache_valid(disk_cache[t])
        if has_cache:
            broker_data = disk_cache[t]["data"]
            cand["broker_source"] = "cached"
        elif api_remaining() > 0:
            broker_data = fetch_broker_summary(t)
            cand["broker_source"] = broker_data.get("source", "live")
        else:
            broker_data = None
            cand["broker_source"] = "pending"

        if broker_data:
            flow         = analyze_flow(broker_data)
            flow_quality = score_flow_quality(broker_data)
        else:
            flow         = {"signal": "No broker data — check quota/cache", "eligible": False, "category": "unknown"}
            flow_quality = {"quality_score": 0, "label": "Pending"}

        cand["flow"]         = flow
        cand["flow_quality"] = flow_quality

        # ── Weekly alignment ──────────────────────────────────────────────────
        weekly = check_weekly_alignment(t_jk)
        cand["weekly"] = weekly

        # ── Relative strength vs IHSG ─────────────────────────────────────────
        rs = compute_relative_strength(t_jk)
        cand["rs"] = rs

        # ── ATR-based SL/TP ───────────────────────────────────────────────────
        cand["atr_levels"] = (
            compute_atr_levels(df_1d, cand["price"])
            if df_1d is not None and len(df_1d) >= 15 else {}
        )

        # ── Composite score ───────────────────────────────────────────────────
        composite    = compute_composite_score(
            tech_score=cand["tech_score"], smc=cand["smc"],
            flow=flow, flow_quality=flow_quality, weekly=weekly, rs=rs,
        )
        cand["composite"] = composite
        cand["score"]     = composite["composite_score"]

        # ── IHSG gate: demote to caution when market is Bear ─────────────────────
        if not ihsg_daily_ok:
            caution.append(cand)
            continue

        # ── Flow gate: route into the correct bucket ───────────────────────────
        if broker_data is None:
            watch.append(cand)                           # no data yet
        elif broker_data.get("source") == "simulated":
            watch.append(cand)                           # simulated ≠ real signal
        elif flow.get("eligible"):
            confirmed.append(cand)                       # ✓ smart money confirmed
        else:
            caution.append(cand)                         # ✗ retail / bad flow

    confirmed.sort(key=lambda x: x["score"], reverse=True)
    caution.sort(  key=lambda x: x["score"], reverse=True)

    # ── Build full watchlist for ALL universe stocks ───────────────────────────
    # Uses the already-downloaded batch data — no extra network calls.
    # Broker flow for watchlist comes from disk cache only (no GoAPI spend).
    watchlist = []
    for t in universe:
        t_jk = f"{t}.JK"
        try:
            if isinstance(data_1d.columns, pd.MultiIndex):
                if t_jk not in data_1d.columns.get_level_values(0):
                    continue
                df_1d_w = data_1d[t_jk].dropna()
                df_1h_w = data_1h[t_jk].dropna() if t_jk in data_1h.columns.get_level_values(0) else pd.DataFrame()
            else:
                df_1d_w = data_1d.dropna()
                df_1h_w = data_1h.dropna()

            if df_1d_w.empty:
                continue

            # Basic trend stats
            if len(df_1d_w) >= 20:
                tv_w = validate_trend_volume(df_1d_w)
            else:
                tv_w = {
                    "current_price": float(df_1d_w["Close"].iloc[-1]),
                    "ma20": 0, "trend_valid": False, "vol_valid": False,
                    "current_vol": 0, "avg_vol_10d": 1,
                }

            # 4H SMC bias
            smc_bias_w = "N/A"
            near_poi_w = False
            poi_low_w  = poi_high_w = 0
            try:
                if len(df_1h_w) >= 20:
                    df_4h_w = df_1h_w.resample("4h").agg({
                        "Open": "first", "High": "max", "Low": "min",
                        "Close": "last", "Volume": "sum",
                    }).dropna()
                    if len(df_4h_w) >= 10:
                        smc_w      = extract_smc(df_4h_w, "4H")
                        smc_bias_w = smc_w.get("bias", "Neutral")
                        poi_low_w  = smc_w.get("poi_low",  0)
                        poi_high_w = smc_w.get("poi_high", 0)
                        cp         = tv_w["current_price"]
                        near_poi_w = (poi_low_w > 0 and poi_high_w > 0
                                      and cp <= poi_high_w * 1.03
                                      and cp >= poi_low_w  * 0.97)
            except Exception:
                pass

            # Broker flow — disk cache only, never call GoAPI for watchlist
            cached_entry = disk_cache.get(t)
            if cached_entry and _broker_cache_valid(cached_entry):
                bd_w         = cached_entry["data"]
                flow_w       = analyze_flow(bd_w)
                flow_elig_w  = flow_w.get("eligible", False)
                flow_cat_w   = flow_w.get("category", "unknown")
                flow_sig_w   = flow_w.get("signal", "")[:60]
                flow_src_w   = "cached"
            else:
                flow_elig_w  = None      # Unknown — no cached data
                flow_cat_w   = "no_cache"
                flow_sig_w   = "No broker cache"
                flow_src_w   = "none"

            avg_vol = max(tv_w.get("avg_vol_10d", 1), 1)
            vol_ratio = round(tv_w.get("current_vol", 0) / avg_vol, 1)

            watchlist.append({
                "ticker":       t,
                "price":        tv_w["current_price"],
                "ma20":         tv_w.get("ma20", 0),
                "above_ma20":   tv_w.get("trend_valid", False),
                "vol_valid":    tv_w.get("vol_valid",   False),
                "vol_ratio":    vol_ratio,
                "smc_bias":     smc_bias_w,
                "near_poi":     near_poi_w,
                "poi_low":      round(poi_low_w,  2),
                "poi_high":     round(poi_high_w, 2),
                "flow_eligible": flow_elig_w,
                "flow_category": flow_cat_w,
                "flow_signal":   flow_sig_w,
                "flow_source":   flow_src_w,
                "is_hot":        t in passed_technical_set,
            })
        except Exception:
            continue

    # Sort: hot signals first → above MA20 → alphabetical
    watchlist.sort(key=lambda x: (
        -int(x["is_hot"]),
        -int(x["above_ma20"]),
        x["ticker"],
    ))

    api_status = get_api_status()
    session    = is_valid_trading_window()

    # ── Persist today's signals to the daily trade log ───────────────────────
    # Save confirmed + watch so ALL grades appear in the log.
    # confirmed = smart money verified (Grade A/B likely)
    # watch     = no broker data yet — still valid technical setups (Grade B/C)
    signals_to_log = confirmed + watch
    if signals_to_log:
        try:
            from . import trade_log as _tl
            _tl.save_daily_signals(signals_to_log, datetime.date.today().isoformat())
        except Exception:
            pass  # never crash the screener if trade_log has an issue

    res = {
        # Signal buckets (all passed technical filter, split by flow)
        "candidates":           confirmed + watch + caution,  # backward compat
        "confirmed":            confirmed,
        "watch":                watch,
        "caution":              caution,
        "total_candidates":     len(confirmed) + len(watch) + len(caution),
        "with_flow_data":       len(confirmed) + len(caution),
        "pending_flow":         len(watch),
        # Full universe watchlist
        "watchlist":            watchlist,
        # Meta
        "universe_size":        len(universe),
        "fca_excluded":         len(IDX_UNIVERSE) - len(universe),
        "api_calls_today":      api_status["calls_today"],
        "api_calls_remaining":  api_status["calls_remaining"],
        "trading_session":      session,
    }
    _cache_set(cache_key, res)
    return res


# ═══════════════════════════════════════════════════════════════════════════════
# STRATEGY ENHANCEMENTS — v2 additions
# ═══════════════════════════════════════════════════════════════════════════════

# ── 1. ATR-Based Dynamic SL/TP ───────────────────────────────────────────────
def compute_atr_levels(records: list, entry_price: float, atr_period: int = 14,
                       atr_sl_mult: float = 1.5, rr: float = 2.5) -> dict:
    """
    Replace the hardcoded 1.5% SL with a volatility-aware stop based on ATR(14).
    SL = entry - ATR(14) * atr_sl_mult  (long bias)
    TP = entry + (entry - SL) * rr

    Why: A fixed 1.5% stop ignores whether the stock moves 0.5%/day or 3%/day.
    ATR anchors the stop to actual realised volatility.
    """
    if len(records) < atr_period + 1:
        return {"atr": None, "sl": None, "tp": None, "rr": rr, "method": "atr"}

    df = pd.DataFrame(records) if not isinstance(records, pd.DataFrame) else records.copy()
    high  = df["High"].astype(float)
    low   = df["Low"].astype(float)
    close = df["Close"].astype(float)

    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low  - prev_close).abs()
    ], axis=1).max(axis=1)

    atr = float(tr.rolling(atr_period).mean().iloc[-1])
    sl  = round(entry_price - atr * atr_sl_mult, 2)
    tp  = round(entry_price + (entry_price - sl) * rr, 2)
    sl_pct = round((entry_price - sl) / entry_price * 100, 2)

    return {
        "atr":    round(atr, 2),
        "sl":     sl,
        "tp":     tp,
        "rr":     rr,
        "sl_pct": sl_pct,
        "method": "atr",
    }


# ── 2. Higher Timeframe (Weekly) Alignment Check ─────────────────────────────
def check_weekly_alignment(ticker_jk: str) -> dict:
    """
    Only take bullish 4H setups when the weekly trend agrees.
    Fetches 1-year weekly data and checks:
      - Price > EMA20W (trend filter)
      - EMA20W slope is rising (momentum filter)
    Returns: {"aligned": bool, "bias": "Bullish"|"Bearish"|"Neutral", "ema20w": float}

    Why: A bullish 4H structure inside a weekly downtrend is a counter-trend trade.
    Professional SMC requires top-down alignment: Monthly → Weekly → Daily → 4H → 1H.
    """
    cache_key = f"weekly_{ticker_jk}"
    cached = _cache_get(cache_key)
    if cached:
        return cached

    try:
        df = yf.download(ticker_jk, period="2y", interval="1wk", progress=False, auto_adjust=True)
        if df.empty or len(df) < 21:
            return {"aligned": False, "bias": "Neutral", "ema20w": 0}

        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        close = df["Close"].astype(float)
        ema20 = close.ewm(span=20, adjust=False).mean()
        current = float(close.iloc[-1])
        ema_now = float(ema20.iloc[-1])
        ema_prev = float(ema20.iloc[-2])

        slope_rising = ema_now > ema_prev
        above_ema = current > ema_now

        if above_ema and slope_rising:
            bias = "Bullish"
        elif not above_ema and not slope_rising:
            bias = "Bearish"
        else:
            bias = "Neutral"

        result = {
            "aligned":     (bias == "Bullish"),
            "bias":        bias,
            "ema20w":      round(ema_now, 2),
            "slope_rising": slope_rising,
            "above_ema":   above_ema,
        }
        _cache_set(cache_key, result)
        return result
    except Exception as e:
        return {"aligned": False, "bias": "Neutral", "ema20w": 0, "error": str(e)}


# ── 3. Relative Strength vs IHSG ─────────────────────────────────────────────
def compute_relative_strength(ticker_jk: str, period: int = 20) -> dict:
    """
    Measures whether the stock is outperforming IHSG (^JKSE) over N days.
    RS = (stock return over N days) - (IHSG return over N days)
    Positive RS = stock is stronger than the market (institutional rotation signal).

    Why: Even in a bull market, money flows into sector leaders.
    Stocks with positive RS tend to break out first and hold gains longer.
    """
    cache_key = f"rs_{ticker_jk}_{period}"
    cached = _cache_get(cache_key)
    if cached:
        return cached

    try:
        tickers = f"{ticker_jk} ^JKSE"
        df = yf.download(tickers, period="3mo", interval="1d",
                         progress=False, auto_adjust=True)

        if isinstance(df.columns, pd.MultiIndex):
            close = df["Close"]
        else:
            return {"rs": 0, "outperforming": False}

        ticker_sym = ticker_jk
        if ticker_sym not in close.columns or "^JKSE" not in close.columns:
            return {"rs": 0, "outperforming": False}

        stock_ret = float(close[ticker_sym].iloc[-1] / close[ticker_sym].iloc[-period] - 1)
        ihsg_ret  = float(close["^JKSE"].iloc[-1]  / close["^JKSE"].iloc[-period]  - 1)
        rs = round((stock_ret - ihsg_ret) * 100, 2)

        result = {
            "rs":            rs,
            "stock_return":  round(stock_ret * 100, 2),
            "ihsg_return":   round(ihsg_ret  * 100, 2),
            "outperforming": (rs > 0),
            "period_days":   period,
        }
        _cache_set(cache_key, result)
        return result
    except Exception as e:
        return {"rs": 0, "outperforming": False, "error": str(e)}


# ── 4. Retail Contamination Score ─────────────────────────────────────────────
def score_flow_quality(broker_data: dict) -> dict:
    """
    Penalise signals where retail brokers appear in the top-3 buyers
    even when the overall category is "foreign" or "bumn".

    Returns a quality score 0–100:
      100 = pure Tier-1 foreign accumulation
       75 = clean foreign (no retail in top-3)
       50 = foreign + retail mixed in top-3
       25 = BUMN only
        0 = retail dominated

    Why: The current cascade logic labels BRPT and CPIN as "foreign"
    even though XC (retail) appears as one of the top-3 buyers.
    This metric surfaces the contamination without breaking existing logic.
    """
    raw = broker_data.get("data", {})
    buy_list = raw.get("buy", [])[:3]
    if not buy_list:
        return {"quality_score": 0, "label": "No Data", "contaminated": False}

    top3 = [b["broker"].upper() for b in buy_list]
    tier1_count  = sum(b in TIER1_FOREIGN  for b in top3)
    foreign_count = sum(b in ALL_FOREIGN   for b in top3)
    retail_count  = sum(b in RETAIL_FLAGS  for b in top3)
    bumn_count    = sum(b in LOCAL_BUMN    for b in top3)

    contaminated = retail_count > 0 and foreign_count > 0

    if tier1_count >= 2 and retail_count == 0:
        score, label = 100, "Pure Tier-1 Conviction"
    elif foreign_count >= 2 and retail_count == 0:
        score, label = 75,  "Clean Foreign Flow"
    elif foreign_count >= 2 and retail_count == 1:
        score, label = 50,  "Foreign (Retail Contaminated)"
    elif bumn_count >= 1 and retail_count == 0:
        score, label = 40,  "Institutional (BUMN)"
    elif retail_count >= 2:
        score, label = 10,  "Retail Dominated — Avoid"
    else:
        score, label = 25,  "Mixed — Low Confidence"

    return {
        "quality_score": score,
        "label":         label,
        "contaminated":  contaminated,
        "top3":          top3,
        "tier1_count":   tier1_count,
        "foreign_count": foreign_count,
        "retail_count":  retail_count,
    }


# ── 5. Fixed-Fractional Position Sizing ──────────────────────────────────────
def compute_position_size(account_equity: float, entry_price: float,
                           sl_price: float, risk_pct: float = 1.0) -> dict:
    """
    Calculates how many shares/lots to buy so that if SL is hit,
    the account loses exactly `risk_pct`% of equity.

    Formula: Position Size = (Equity × Risk%) / (Entry - SL)

    IDX uses lots of 100 shares.

    Why: Without position sizing, a Rp 394 stock (ACES) and a Rp 5,575 stock (INTP)
    imply very different capital commitments and risk exposures if bought
    in equal lot counts. Fixed-fractional equalises risk across all positions.
    """
    if entry_price <= sl_price or account_equity <= 0:
        return {"error": "Invalid inputs"}

    risk_idr     = account_equity * (risk_pct / 100)
    risk_per_shr = entry_price - sl_price
    shares_raw   = risk_idr / risk_per_shr
    lots         = max(1, int(shares_raw / 100))   # round down to whole lots
    shares       = lots * 100
    capital_used = shares * entry_price
    actual_risk  = shares * risk_per_shr

    return {
        "lots":          lots,
        "shares":        shares,
        "capital_idr":   round(capital_used, 0),
        "risk_idr":      round(actual_risk, 0),
        "risk_pct_actual": round(actual_risk / account_equity * 100, 2),
        "account_equity": account_equity,
        "risk_target_pct": risk_pct,
    }


# ── 6. IDX Session Time Filter ────────────────────────────────────────────────
def is_valid_trading_window() -> dict:
    """
    IDX Sessions:
      Pre-open:  08:45–09:00 (avoid — auction period, erratic prices)
      Session 1: 09:00–12:00 (best liquidity, first 30 min volatile)
      Break:     12:00–13:30
      Session 2: 13:30–15:49 (closing auction 15:50–16:00 — avoid)

    Returns whether current WIB time is in a valid entry window.
    Avoid: first 30 min, last 20 min of each session, and break.

    Why: SMC signals generated during the opening auction or pre-close
    manipulation period have significantly higher failure rates.
    """
    import datetime, pytz

    wib = pytz.timezone("Asia/Jakarta")
    now = datetime.datetime.now(wib).time()

    valid_windows = [
        (datetime.time(9, 30),  datetime.time(11, 50)),   # Mid-morning
        (datetime.time(14,  0), datetime.time(15, 30)),   # Mid-afternoon
    ]

    in_window = any(start <= now <= end for start, end in valid_windows)
    session = "Break" if datetime.time(12, 0) <= now < datetime.time(13, 30) else \
              "Pre-open" if now < datetime.time(9, 0) else \
              "Session 1" if now < datetime.time(12, 0) else \
              "Session 2" if now < datetime.time(15, 50) else "Closed"

    return {
        "in_valid_window": in_window,
        "session":         session,
        "current_wib":     now.strftime("%H:%M"),
    }


# ── 7. Composite Signal Score (v2) ────────────────────────────────────────────
def compute_composite_score(
    tech_score: int,
    smc: dict,
    flow: dict,
    flow_quality: dict,
    weekly: dict,
    rs: dict,
) -> dict:
    """
    Enhanced scoring model that combines all layers with proper weighting.

    Scoring breakdown (max 100):
      Technical structure (SMC):  25 pts
      Trend + Volume flags:       20 pts  (from tech_score normalization)
      Weekly alignment:           15 pts
      Relative strength vs IHSG:  10 pts
      Flow eligibility:           15 pts
      Flow quality (no retail):   15 pts

    Why: The original score is 0–100 from tech_score alone, with flow
    appended as a binary eligible/not-eligible. This conflates very
    different quality signals into one number. The composite score
    separately weights each dimension so a high-quality clean signal
    scores higher than a noisy one.
    """
    score = 0
    breakdown = {}

    # Technical structure: BOS confirmed + near POI
    smc_pts = 0
    if smc.get("bias") in ("Bullish", "Bearish"):
        smc_pts += 15
    if smc.get("rr", 0) >= 2.5:
        smc_pts += 10
    score += smc_pts
    breakdown["smc"] = smc_pts

    # Trend / Volume (normalise original 0-100 score to 0-20)
    tv_pts = int(min(tech_score, 100) / 100 * 20)
    score += tv_pts
    breakdown["trend_volume"] = tv_pts

    # Weekly alignment
    weekly_pts = 15 if weekly.get("aligned") else 0
    score += weekly_pts
    breakdown["weekly_alignment"] = weekly_pts

    # Relative strength
    rs_pts = 10 if rs.get("outperforming") else 0
    score += rs_pts
    breakdown["relative_strength"] = rs_pts

    # Flow eligibility
    flow_pts = 15 if flow.get("eligible") else 0
    score += flow_pts
    breakdown["flow_eligible"] = flow_pts

    # Flow quality (penalise retail contamination)
    quality_raw = flow_quality.get("quality_score", 0)  # 0-100
    quality_pts = int(quality_raw / 100 * 15)
    score += quality_pts
    breakdown["flow_quality"] = quality_pts

    # ── V3.9 Confluence-based grade (10 binary signals → 0.0–1.0) ─────────────
    # Mirrors the backtest's confluence gate so live screener grades align with
    # what the backtest would assign.  Each signal is 0 or 1.
    # (quality_raw already assigned above for the composite score computation)
    conf_signals = [
        smc.get("bias") == "Bullish",          # 1. SMC 1H structure bullish
        bool(smc.get("bos")),                  # 2. Break of Structure confirmed
        bool(smc.get("idm", 0)),               # 3. IDM liquidity sweep (smart money)
        float(smc.get("rr", 0)) >= 2.5,        # 4. Risk:Reward ≥ 2.5
        bool(weekly.get("aligned")),            # 5. Weekly HTF trend aligned
        bool(rs.get("outperforming")),          # 6. Outperforming IHSG (relative strength)
        bool(flow.get("eligible")),             # 7. Smart money flow confirmed
        int(quality_raw) >= 50,                # 8. Institutional flow quality ≥ 50
        tech_score >= 115,                      # 9. 4H bullish bias active (max tech score)
        int(quality_raw) >= 70,                # 10. Premium flow (foreign/BUMN dominant)
    ]
    confluence = round(sum(1 for s in conf_signals if s) / 10.0, 2)
    grade = ("A" if confluence >= 0.80
             else "B" if confluence >= 0.60
             else "C" if confluence >= 0.40
             else "D")

    return {
        "composite_score":  score,
        "grade":            grade,
        "confluence_score": confluence,
        "breakdown":        breakdown,
        "max_score":        85,   # 25+20+15+10+15+15 — reflects real ceiling
    }
