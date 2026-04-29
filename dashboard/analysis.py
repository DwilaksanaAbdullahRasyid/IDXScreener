"""
analysis.py — All financial logic for the IDX Screener:
  - IHSG bull/bear scoring
  - Yahoo Finance OHLCV fetching & 4H resampling
  - GoAPI broker summary fetching (with persistent 24h cache + daily quota tracker)
  - Follow-the-Giant broker classification
  - SMC (Smart Money Concept) swing structure detection
  - Accumulation score calculation
  - FCA / suspended stock exclusion (fetched live from IDX, hardcoded fallback)
"""

import json
import numpy as np
import pandas as pd
import yfinance as yf
import requests
import random
import time
import datetime
from functools import lru_cache
from pathlib import Path

import os
from dotenv import load_dotenv

load_dotenv()

# ── API Key ──────────────────────────────────────────────────────────────────
GOAPI_KEY  = os.getenv("GOAPI_KEY")
GOAPI_BASE = "https://api.goapi.io"

# ── Quota & Cache Config ──────────────────────────────────────────────────────
DAILY_API_LIMIT   = 28          # stay 2 under the 30/day hard limit
BROKER_CACHE_TTL  = 86400       # 24 hours — broker data is daily
OHLCV_CACHE_TTL   = 3600        # 1 hour for price data

BASE_DIR          = Path(__file__).resolve().parent.parent
BROKER_CACHE_FILE = BASE_DIR / "broker_cache.json"
API_USAGE_FILE    = BASE_DIR / "api_usage.json"

# ── Stock Universe ────────────────────────────────────────────────────────────
# LQ45 (as of early 2026 — BREN/DSSA removed from IDX80 in May 2026)
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
    "AUTO", "SMSM", "JPFA", "TURI", "ABMM", "BISI",
    # New Economy / EV Metals
    "AMMN", "MBMA", "NCKL",
    # Entertainment / Creative
    "FILM",
]

# Full universe: ~87 liquid IDX stocks
IDX_UNIVERSE = LQ45 + [t for t in IDX_ADDITIONAL if t not in LQ45]

# ── Broker Lists ─────────────────────────────────────────────────────────────
ALL_FOREIGN  = ["YU","CG","KZ","CS","DP","GW","BK","DU","HD","AG","BQ",
                "RX","ZP","MS","XA","RB","TP","LS","DR","LH","AH","LG",
                "AK","AI","FS"]
TIER1_FOREIGN = ["AK", "BK", "KZ"]
LOCAL_BUMN    = ["CC", "NI", "OD", "DX"]
RETAIL_FLAGS  = ["YP", "XC", "XL", "MG", "AZ", "KK"]
ALL_LOCAL     = ["XC","PP","YO","ID","SH","BZ","AQ","AR","GA","SA","RF","ZR",
                 "KI","PF","II","TX","TS","ES","MK","BS","AO","EL","PC","FO",
                 "AF","HP","SC","IU","PD","IP","BF","IT","IN","YB","KS","YJ",
                 "XL","GI","DD","DM","CD","MU","EP","OK","RO","IH","AP","PG",
                 "GR","PS","AT","PO","RG","IF","MG","DH","AZ","SS","SF","BR",
                 "TF","CP","BB","MI","AN","FZ","RS","AD","PI","QA","ZZ","JB"]

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

def _get_broker_from_disk(ticker: str) -> dict | None:
    cache = _load_broker_disk()
    entry = cache.get(ticker)
    if entry and (time.time() - entry["ts"]) < BROKER_CACHE_TTL:
        return entry["data"]
    return None

def _put_broker_to_disk(ticker: str, data: dict):
    cache = _load_broker_disk()
    cache[ticker] = {"ts": time.time(), "data": data}
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
    cached_tickers = [t for t, v in disk.items() if (now - v["ts"]) < BROKER_CACHE_TTL]
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
# Known FCA/suspended tickers — used as fallback if IDX API unreachable.
# This list should be updated periodically; as of April 2026 IDX reports ~173 FCA stocks.
# These are mostly small-cap penny stocks unlikely to appear in IDX_UNIVERSE.
_KNOWN_FCA_FALLBACK: set[str] = {
    "ATAP", "RONY", "TELE", "JSKY", "IATA", "BPTR", "FUJI", "MYOH",
    "SMKL", "FIRE", "NELY", "TAXI", "SUGI", "MITI", "DIGI",
}

def fetch_fca_suspended_stocks() -> set:
    """
    Fetches the live FCA / special-monitoring stock list from IDX.
    Falls back to _KNOWN_FCA_FALLBACK if the request fails.
    Result is cached in memory for 6 hours.
    """
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

# ── IHSG Bull/Bear Score ──────────────────────────────────────────────────────
def fetch_ihsg():
    cached = _cache_get("ihsg")
    if cached:
        return cached
    try:
        df = yf.download("^JKSE", period="1y", interval="1d", progress=False, auto_adjust=True)
        if df.empty:
            return {"score": 0, "status": "No Data", "current": 0, "ma20": 0, "ma50": 0, "ma200": 0}

        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        if "Close" not in df.columns:
            return {"score": 0, "status": "Error: No Close Data", "current": 0, "ma20": 0, "ma50": 0, "ma200": 0}

        close = df["Close"].dropna()
        ma200_val = close.rolling(200).mean().iloc[-1] if len(close) >= 200 else close.mean()

        current = float(close.iloc[-1])
        ma20    = float(close.rolling(20).mean().iloc[-1])
        ma50    = float(close.rolling(50).mean().iloc[-1])
        ma200   = float(ma200_val)

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
            "ma20": round(ma20, 2), "ma50": round(ma50, 2), "ma200": round(ma200, 2),
        }
        _cache_set("ihsg", result)
        return result
    except Exception as e:
        print(f"IHSG fetch error: {e}")
        return {"score": 0, "status": "Error Loading Data", "current": 0, "ma20": 0, "ma50": 0, "ma200": 0, "error": str(e)}

# ── Yahoo Finance OHLCV ───────────────────────────────────────────────────────
def _df_to_records(df: pd.DataFrame) -> list:
    if df.empty:
        return []
    df = df.copy()
    df.index = df.index.astype(str)
    df = df.where(pd.notnull(df), None)
    return df.reset_index().rename(columns={"index": "Date"}).to_dict(orient="records")

def fetch_ohlcv(ticker: str):
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
            "Close": "last", "Volume": "sum",
        }).dropna()

        result["data_1d"] = _df_to_records(raw_1d[["Open","High","Low","Close","Volume"]])
        result["data_4h"] = _df_to_records(raw_4h)
        result["data_1h"] = _df_to_records(raw_1h[["Open","High","Low","Close","Volume"]])

        _cache_set(cache_key, result)
    except Exception as e:
        result["error"] = str(e)
    return result

# ── GoAPI Broker Summary ──────────────────────────────────────────────────────
def fetch_broker_summary(ticker: str):
    """
    Fetch broker buy/sell data.

    Priority:
      1. In-memory cache (1h)
      2. Persistent disk cache (24h)
      3. Live GoAPI call (if daily quota allows)
      4. Simulated fallback
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

    # 3. Live GoAPI call — only if quota available
    if api_remaining() <= 0:
        sim = _simulate_broker_data(ticker)
        sim["error"] = "Daily GoAPI quota exhausted. Showing simulated data."
        sim["quota_exhausted"] = True
        _cache_set(mem_key, sim)
        return sim

    url     = f"{GOAPI_BASE}/stock/idx/{ticker}/broker_summary"
    headers = {"X-API-KEY": GOAPI_KEY}
    try:
        r = requests.get(url, headers=headers, timeout=8)
        if r.status_code == 200:
            data = r.json()
            if "data" in data and "results" in data["data"]:
                results = data["data"]["results"]

                if not results:
                    yesterday = (datetime.date.today() - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
                    r2 = requests.get(f"{url}?date={yesterday}", headers=headers, timeout=8)
                    if r2.status_code == 200:
                        y_data = r2.json()
                        if "data" in y_data and "results" in y_data["data"]:
                            results = y_data["data"]["results"]

                if results:
                    buy_list, sell_list = [], []
                    for row in results:
                        broker_code = row.get("code") or (row.get("broker") or {}).get("code")
                        entry = {
                            "broker": broker_code,
                            "vol":    row.get("lot", 0),
                            "val":    row.get("value", 0),
                            "avg":    row.get("avg", 0),
                        }
                        side = row.get("side", "").upper()
                        if side == "BUY":
                            buy_list.append(entry)
                        elif side == "SELL":
                            sell_list.append(entry)

                    buy_list.sort(key=lambda x: x["vol"], reverse=True)
                    sell_list.sort(key=lambda x: x["vol"], reverse=True)
                    total_vol = sum(b["vol"] for b in buy_list) + sum(s["vol"] for s in sell_list)

                    formatted = {
                        "source":    "live",
                        "data":      {"buy": buy_list, "sell": sell_list, "total_vol": total_vol},
                        "error":     None,
                        "quota_exhausted": False,
                    }
                    _increment_api_usage(ticker)
                    _cache_set(mem_key, formatted)
                    _put_broker_to_disk(ticker, formatted)
                    return formatted

        if r.status_code != 200:
            raise ValueError(f"GoAPI status {r.status_code}")
        raise ValueError("Empty data returned from GoAPI even with fallback.")

    except Exception as e:
        sim = _simulate_broker_data(ticker)
        sim["error"] = f"GoAPI unavailable ({e}). Showing simulated data."
        sim["quota_exhausted"] = False
        _cache_set(mem_key, sim)
        return sim

def _simulate_broker_data(ticker: str) -> dict:
    rng = random.Random(ticker + str(int(time.time() // 3600)))
    buy_brokers  = rng.sample(ALL_FOREIGN[:10] + LOCAL_BUMN + RETAIL_FLAGS, 8)
    sell_brokers = rng.sample(ALL_LOCAL[:10] + RETAIL_FLAGS, 6)
    total_vol    = rng.randint(5_000_000, 50_000_000)

    def make_entries(brokers):
        entries = []
        for b in brokers:
            vol = rng.randint(100_000, int(total_vol * 0.3))
            val = vol * rng.randint(2000, 12000)
            entries.append({"broker": b.upper(), "vol": vol, "val": val, "avg": val // max(vol, 1)})
        entries.sort(key=lambda x: x["vol"], reverse=True)
        return entries

    return {
        "source": "simulated",
        "data":   {"buy": make_entries(buy_brokers), "sell": make_entries(sell_brokers), "total_vol": total_vol},
        "error":  None,
    }

# ── Follow-the-Giant Analysis ─────────────────────────────────────────────────
def analyze_flow(broker_data: dict) -> dict:
    raw       = broker_data.get("data", {})
    buy_list  = raw.get("buy", [])
    total_vol = raw.get("total_vol", sum(b.get("vol", 0) for b in buy_list))

    if not buy_list:
        return {"signal": "No Data", "eligible": False}

    top3       = buy_list[:3]
    top3_names = [b["broker"].upper() for b in top3]
    top3_vol   = sum(b.get("vol", 0) for b in top3)
    acc_score  = top3_vol / total_vol if total_vol > 0 else 0

    is_tier1   = [n in TIER1_FOREIGN for n in top3_names]
    is_foreign = [n in ALL_FOREIGN   for n in top3_names]
    is_bumn    = [n in LOCAL_BUMN    for n in top3_names]
    is_retail  = [n in RETAIL_FLAGS  for n in top3_names]

    tier1_count   = sum(is_tier1)
    foreign_count = sum(is_foreign)
    retail_count  = sum(is_retail)
    bumn_count    = sum(is_bumn)

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

    if acc_score >= 0.5:
        acc_label = "🐋 Massive Accumulation (Whale at work!)"
    elif acc_score >= 0.3:
        acc_label = "📈 Accumulation Detected"
    else:
        acc_label = "Normal Activity"

    return {
        "top3":          [{"broker": n, "vol": b.get("vol", 0), "val": b.get("val", 0)}
                          for n, b in zip(top3_names, top3)],
        "top3_names":    top3_names,
        "acc_score":     round(acc_score * 100, 1),
        "acc_label":     acc_label,
        "signal":        signal,
        "category":      category,
        "eligible":      eligible,
        "foreign_count": foreign_count,
        "bumn_count":    bumn_count,
        "retail_count":  retail_count,
    }

# ── SMC — Smart Money Concept ─────────────────────────────────────────────────
def extract_smc(records: list, timeframe: str = "1H") -> dict:
    if len(records) < 20:
        return {"bias": "Neutral", "error": "Not enough data"}

    if isinstance(records, pd.DataFrame):
        df = records.copy()
        if "Date" in df.columns:
            df = df.rename(columns={"Date": "date"})
        elif "Datetime" in df.columns:
            df = df.rename(columns={"Datetime": "date"})
        elif df.index.name in ["Date", "Datetime"] or isinstance(df.index, pd.DatetimeIndex):
            df["date"] = df.index.astype(str)
        else:
            df["date"] = df.index.astype(str)
    else:
        df = pd.DataFrame(records)
        df = df.rename(columns={"Date": "date"})

    highs  = df["High"].astype(float).values
    lows   = df["Low"].astype(float).values
    closes = df["Close"].astype(float).values

    window = 5
    swing_highs, swing_lows = [], []
    for i in range(window, len(df) - window):
        if highs[i] == np.max(highs[i - window: i + window + 1]):
            swing_highs.append({"idx": i, "price": float(highs[i]), "date": str(df["date"].iloc[i])})
        if lows[i] == np.min(lows[i - window: i + window + 1]):
            swing_lows.append({"idx": i, "price": float(lows[i]), "date": str(df["date"].iloc[i])})

    if len(swing_highs) < 2 or len(swing_lows) < 2:
        return {"bias": "Neutral", "error": "Insufficient swing points",
                "swing_highs": swing_highs, "swing_lows": swing_lows}

    last_sh = swing_highs[-1]["price"]
    prev_sh = swing_highs[-2]["price"]
    last_sl = swing_lows[-1]["price"]
    prev_sl = swing_lows[-2]["price"]
    current_price = float(closes[-1])

    if last_sh > prev_sh and last_sl > prev_sl:
        bias = "Bullish"
    elif last_sh < prev_sh and last_sl < prev_sl:
        bias = "Bearish"
    else:
        bias = "Neutral"

    if bias == "Bullish":
        bos      = prev_sh
        choch    = prev_sl
        fib_range = last_sh - last_sl
        poi_low  = last_sl + fib_range * 0.0
        poi_high = last_sl + fib_range * 0.5
        idm      = swing_lows[-2]["price"] if len(swing_lows) >= 2 else last_sl
        eqh, eql = prev_sh, prev_sl
        entry    = round((poi_low + poi_high) / 2, 2)
        sl       = round(poi_low * 0.985, 2)
        tp       = round(entry + (entry - sl) * 2.5, 2)
    elif bias == "Bearish":
        bos      = prev_sl
        choch    = prev_sh
        fib_range = last_sh - last_sl
        poi_high = last_sh - fib_range * 0.0
        poi_low  = last_sh - fib_range * 0.5
        idm      = swing_highs[-2]["price"] if len(swing_highs) >= 2 else last_sh
        eqh, eql = prev_sh, prev_sl
        entry    = round((poi_low + poi_high) / 2, 2)
        sl       = round(poi_high * 1.015, 2)
        tp       = round(entry - (sl - entry) * 2.5, 2)
    else:
        bos = choch = poi_low = poi_high = idm = 0
        eqh, eql = last_sh, last_sl
        entry = sl = tp = 0

    rr = round(abs(tp - entry) / abs(entry - sl), 2) if sl != entry else 0

    return {
        "timeframe":   timeframe,
        "bias":        bias,
        "bos":         round(bos, 2),
        "choch":       round(choch, 2),
        "poi_low":     round(poi_low, 2),
        "poi_high":    round(poi_high, 2),
        "idm":         round(idm, 2),
        "eqh":         round(eqh, 2),
        "eql":         round(eql, 2),
        "entry":       entry,
        "sl":          sl,
        "tp":          tp,
        "rr":          rr,
        "current":     round(current_price, 2),
        "swing_highs": swing_highs[-5:],
        "swing_lows":  swing_lows[-5:],
        "error":       None,
    }

# ── Trend + Volume Validation ──────────────────────────────────────────────────
def validate_trend_volume(records_1d: list) -> dict:
    if len(records_1d) < 20:
        return {"trend_valid": False, "vol_valid": False}
    df    = records_1d if isinstance(records_1d, pd.DataFrame) else pd.DataFrame(records_1d)
    close = df["Close"].astype(float)
    vol   = df["Volume"].astype(float)
    ma20      = close.rolling(20).mean().iloc[-1]
    avg10v    = vol.rolling(10).mean().iloc[-1]
    curr_close = float(close.iloc[-1])
    curr_vol   = float(vol.iloc[-1])
    return {
        "trend_valid":    bool(curr_close > ma20),
        "vol_valid":      bool(curr_vol > 1.5 * avg10v),
        "current_price":  round(curr_close, 2),
        "ma20":           round(float(ma20), 2),
        "current_vol":    int(curr_vol),
        "avg_vol_10d":    int(avg10v),
    }

# ── Market Screener ───────────────────────────────────────────────────────────
def screen_market():
    """
    Screens all IDX_UNIVERSE stocks (minus FCA/suspended):
      1. Batch-downloads OHLCV from Yahoo Finance (free, unlimited)
      2. Applies technical + SMC filters — all passing stocks become candidates
      3. For each candidate:
           - Uses 24h persistent broker cache if available (no API cost)
           - Calls GoAPI only if not cached AND daily quota allows (max 28/day)
           - Flags remaining as "pending" broker data
      4. Returns ALL technical candidates sorted by combined score

    With 30 calls/day and 87-stock universe, initial cache is full in ~3 days.
    After that, only top technical setups consume quota each day.
    """
    cache_key = "market_screener"
    cached = _cache_get(cache_key, ttl=1800)   # 30-min cache for screener result
    if cached:
        return cached

    # Exclude FCA / suspended stocks
    fca_set = fetch_fca_suspended_stocks()
    universe = [t for t in IDX_UNIVERSE if t not in fca_set]

    tickers     = [f"{t}.JK" for t in universe]
    tickers_str = " ".join(tickers)

    try:
        data_1d = yf.download(tickers_str, period="3mo", interval="1d",
                               group_by="ticker", progress=False, auto_adjust=True)
        data_1h = yf.download(tickers_str, period="1mo", interval="1h",
                               group_by="ticker", progress=False, auto_adjust=True)
    except Exception as e:
        return {"error": f"Batch download failed: {str(e)}"}

    technical_candidates = []

    for t in universe:
        t_jk = f"{t}.JK"

        # Handle both single-ticker and multi-ticker column shapes
        try:
            if isinstance(data_1d.columns, pd.MultiIndex):
                if t_jk not in data_1d.columns.get_level_values(0):
                    continue
                df_1d = data_1d[t_jk].dropna()
                df_1h = data_1h[t_jk].dropna()
            else:
                # single-ticker batch returns flat columns — shouldn't happen here
                df_1d = data_1d.dropna()
                df_1h = data_1h.dropna()
        except Exception:
            continue

        if len(df_1d) < 20 or len(df_1h) < 20:
            continue

        tv = validate_trend_volume(df_1d)

        df_4h = df_1h.resample("4h").agg({
            "Open": "first", "High": "max", "Low": "min",
            "Close": "last", "Volume": "sum",
        }).dropna()

        smc_4h = extract_smc(df_4h, "4H")

        if smc_4h.get("bias") == "Neutral":
            continue
        if smc_4h.get("bias") != "Bullish":
            continue

        curr_price = tv["current_price"]
        poi_low    = smc_4h.get("poi_low", 0)
        poi_high   = smc_4h.get("poi_high", 0)

        near_poi = (
            poi_low > 0 and poi_high > 0
            and curr_price <= poi_high * 1.03
            and curr_price >= poi_low * 0.97
        )

        if not (tv["trend_valid"] or near_poi):
            continue

        flags = []
        if tv["trend_valid"]: flags.append("Uptrend")
        if tv["vol_valid"]:   flags.append("High Vol")
        if near_poi:          flags.append("Near POI")

        score = (40 if near_poi else 0) + (30 if tv["vol_valid"] else 0) + (30 if tv["trend_valid"] else 0)

        technical_candidates.append({
            "ticker": t,
            "score":  score,
            "price":  curr_price,
            "flags":  flags,
            "smc":    smc_4h,
        })

    # Sort by technical score — best setups get GoAPI budget first
    technical_candidates.sort(key=lambda x: x["score"], reverse=True)

    final_results     = []
    pending_flow      = []   # candidates where GoAPI quota ran out

    disk_cache = _load_broker_disk()
    now        = time.time()

    for cand in technical_candidates:
        t = cand["ticker"]
        has_cache = t in disk_cache and (now - disk_cache[t]["ts"]) < BROKER_CACHE_TTL

        if has_cache:
            broker_data = disk_cache[t]["data"]
            flow        = analyze_flow(broker_data)
            cand["flow"]         = flow
            cand["broker_source"] = broker_data.get("source", "cached")
            cand["broker_cached"] = True
            if flow.get("eligible"):
                cand["score"] += 20
            final_results.append(cand)

        elif api_remaining() > 0:
            broker_data = fetch_broker_summary(t)   # will call GoAPI + save to disk
            flow        = analyze_flow(broker_data)
            cand["flow"]         = flow
            cand["broker_source"] = broker_data.get("source", "live")
            cand["broker_cached"] = False
            if flow.get("eligible"):
                cand["score"] += 20
            final_results.append(cand)

        else:
            cand["flow"]          = {"signal": "⏳ Broker data pending — quota used for today", "eligible": False}
            cand["broker_source"] = "pending"
            cand["broker_cached"] = False
            pending_flow.append(cand)

    # Merge: flow-enriched first, then pending (still useful as technical setups)
    final_results.sort(key=lambda x: x["score"], reverse=True)
    all_candidates = final_results + pending_flow

    api_status = get_api_status()

    res = {
        "candidates":           all_candidates,
        "total_candidates":     len(all_candidates),
        "with_flow_data":       len(final_results),
        "pending_flow":         len(pending_flow),
        "universe_size":        len(universe),
        "fca_excluded":         len(IDX_UNIVERSE) - len(universe),
        "api_calls_today":      api_status["calls_today"],
        "api_calls_remaining":  api_status["calls_remaining"],
    }
    _cache_set(cache_key, res)
    return res
