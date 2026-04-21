"""
analysis.py — All financial logic for the IDX Screener:
  - IHSG bull/bear scoring
  - Yahoo Finance OHLCV fetching & 4H resampling
  - GoAPI broker summary fetching
  - Follow-the-Giant broker classification
  - SMC (Smart Money Concept) swing structure detection
  - Accumulation score calculation
"""

import numpy as np
import pandas as pd
import yfinance as yf
import requests
import random
import time
from functools import lru_cache

import os
from dotenv import load_dotenv

load_dotenv()

# ── API Key ──────────────────────────────────────────────────────────────────
GOAPI_KEY = os.getenv("GOAPI_KEY")
GOAPI_BASE = "https://api.goapi.io"

LQ45 = [
    "ACES", "ADRO", "AKRA", "AMRT", "ANTM", "ARTO", "ASII", "BBCA", 
    "BBNI", "BBRI", "BBTN", "BMRI", "BRIS", "BRPT", "BUKA", "CPIN", 
    "EMTK", "ESSA", "EXCL", "GOTO", "HRUM", "ICBP", "INCO", "INDF", 
    "INKP", "INTP", "ITMG", "KLBF", "MDKA", "MEDC", "PGAS", "PTBA", 
    "SIDO", "SMGR", "SRTG", "TBIG", "TINS", "TLKM", "TOWR", "UNTR", "UNVR"
]

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

# Simple in-process cache: {key: (timestamp, data)}
_cache: dict = {}
CACHE_TTL = 3600  # 1 hour

def _cache_get(key):
    entry = _cache.get(key)
    if entry and (time.time() - entry[0]) < CACHE_TTL:
        return entry[1]
    return None

def _cache_set(key, data):
    _cache[key] = (time.time(), data)

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
def fetch_broker_summary(ticker: str):
    """Fetches broker buy/sell data from GoAPI. Falls back to simulation on failure."""
    cache_key = f"broker_{ticker}"
    cached = _cache_get(cache_key)
    if cached:
        return cached

    url = f"{GOAPI_BASE}/stock/idx/{ticker}/broker_summary"
    headers = {"X-API-KEY": GOAPI_KEY}
    
    # Try fetching data, passing today's or yesterday's date if necessary
    # By default, without date parameter it might return empty or standard summary.
    try:
        r = requests.get(url, headers=headers, timeout=8)
        if r.status_code == 200:
            data = r.json()
            if "data" in data and "results" in data["data"]:
                results = data["data"]["results"]
                
                # If API returned an empty list, let's try explicitly fetching yesterday's data 
                # (since today's data might not be populated during market hours)
                if not results:
                    import datetime
                    yesterday = (datetime.date.today() - datetime.timedelta(days=1)).strftime('%Y-%m-%d')
                    r_yesterday = requests.get(f"{url}?date={yesterday}", headers=headers, timeout=8)
                    if r_yesterday.status_code == 200:
                        y_data = r_yesterday.json()
                        if "data" in y_data and "results" in y_data["data"]:
                            results = y_data["data"]["results"]

                if results:
                    # Transform GoAPI format to our expected format
                    buy_list = []
                    sell_list = []
                    
                    for row in results:
                        broker_code = row.get("code") or (row.get("broker") or {}).get("code")
                        vol = row.get("lot", 0)
                        val = row.get("value", 0)
                        avg = row.get("avg", 0)
                        side = row.get("side", "").upper()
                        
                        entry = {"broker": broker_code, "vol": vol, "val": val, "avg": avg}
                        if side == "BUY":
                            buy_list.append(entry)
                        elif side == "SELL":
                            sell_list.append(entry)
                    
                    # Sort desc by volume
                    buy_list.sort(key=lambda x: x["vol"], reverse=True)
                    sell_list.sort(key=lambda x: x["vol"], reverse=True)
                    
                    total_vol = sum(b["vol"] for b in buy_list) + sum(s["vol"] for s in sell_list)
                    
                    formatted_data = {
                        "buy": buy_list,
                        "sell": sell_list,
                        "total_vol": total_vol
                    }
                    
                    result = {"source": "live", "data": formatted_data, "error": None}
                    _cache_set(cache_key, result)
                    return result
                    
        # Rate limited or error or still empty
        if r.status_code != 200:
            raise ValueError(f"GoAPI status {r.status_code}")
        else:
            raise ValueError("Empty data returned from GoAPI even with fallback.")
    except Exception as e:
        # Simulated fallback
        sim = _simulate_broker_data(ticker)
        sim["error"] = f"GoAPI unavailable ({e}). Showing simulated data."
        _cache_set(cache_key, sim)
        return sim
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
    Detects swing highs/lows, BOS, CHoCH, POI zone, IDM from OHLCV records.
    Returns a structured dict ready for JSON serialization.
    """
    if len(records) < 20:
        return {"bias": "Neutral", "error": "Not enough data"}

    if isinstance(records, pd.DataFrame):
        df = records.copy()
        if "Date" in df.columns:
            df = df.rename(columns={"Date": "date"})
        elif "Datetime" in df.columns:
            df = df.rename(columns={"Datetime": "date"})
        elif df.index.name in ["Date", "Datetime"] or type(df.index) == pd.DatetimeIndex:
            df["date"] = df.index.astype(str)
        else:
            df["date"] = df.index.astype(str)
    else:
        df = pd.DataFrame(records)
        df = df.rename(columns={"Date": "date"})
    highs = df["High"].astype(float).values
    lows  = df["Low"].astype(float).values
    closes = df["Close"].astype(float).values

    window = 5
    swing_highs, swing_lows = [], []

    for i in range(window, len(df) - window):
        if highs[i] == np.max(highs[i - window: i + window + 1]):
            swing_highs.append({"idx": i, "price": float(highs[i]), "date": str(df["date"].iloc[i])})
        if lows[i] == np.min(lows[i - window: i + window + 1]):
            swing_lows.append({"idx": i, "price": float(lows[i]), "date": str(df["date"].iloc[i])})

    if len(swing_highs) < 2 or len(swing_lows) < 2:
        return {"bias": "Neutral", "error": "Insufficient swing points", "swing_highs": swing_highs, "swing_lows": swing_lows}

    last_sh = swing_highs[-1]["price"]
    prev_sh = swing_highs[-2]["price"]
    last_sl = swing_lows[-1]["price"]
    prev_sl = swing_lows[-2]["price"]

    current_price = float(closes[-1])

    # Trend bias
    if last_sh > prev_sh and last_sl > prev_sl:
        bias = "Bullish"
    elif last_sh < prev_sh and last_sl < prev_sl:
        bias = "Bearish"
    else:
        bias = "Neutral"

    # BOS / CHoCH
    if bias == "Bullish":
        bos   = prev_sh   # Price broke above this → BOS
        choch = prev_sl   # If price breaks below this → CHoCH (reversal warning)
        fib_range = last_sh - last_sl
        poi_low  = last_sl + fib_range * 0.0
        poi_high = last_sl + fib_range * 0.5   # Discount zone (below 50%)
        idm  = swing_lows[-2]["price"] if len(swing_lows) >= 2 else last_sl
        eqh  = prev_sh
        eql  = prev_sl
        entry = round((poi_low + poi_high) / 2, 2)
        sl    = round(poi_low * 0.985, 2)
        tp    = round(entry + (entry - sl) * 2.5, 2)
    elif bias == "Bearish":
        bos   = prev_sl
        choch = prev_sh
        fib_range = last_sh - last_sl
        poi_high = last_sh - fib_range * 0.0
        poi_low  = last_sh - fib_range * 0.5   # Premium zone (above 50%)
        idm  = swing_highs[-2]["price"] if len(swing_highs) >= 2 else last_sh
        eqh  = prev_sh
        eql  = prev_sl
        entry = round((poi_low + poi_high) / 2, 2)
        sl    = round(poi_high * 1.015, 2)
        tp    = round(entry - (sl - entry) * 2.5, 2)
    else:
        bos   = 0
        choch = 0
        poi_low  = 0
        poi_high = 0
        idm  = 0
        eqh  = last_sh
        eql  = last_sl
        entry = 0; sl = 0; tp = 0

    rr = round(abs(tp - entry) / abs(entry - sl), 2) if sl != entry else 0

    return {
        "timeframe":    timeframe,
        "bias":         bias,
        "bos":          round(bos, 2),
        "choch":        round(choch, 2),
        "poi_low":      round(poi_low, 2),
        "poi_high":     round(poi_high, 2),
        "idm":          round(idm, 2),
        "eqh":          round(eqh, 2),
        "eql":          round(eql, 2),
        "entry":        entry,
        "sl":           sl,
        "tp":           tp,
        "rr":           rr,
        "current":      round(current_price, 2),
        "swing_highs":  swing_highs[-5:],
        "swing_lows":   swing_lows[-5:],
        "error":        None,
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

# ── Market Screener ──────────────────────────────────────────────────────────
def screen_market():
    """
    Downloads LQ45 stocks in batch, filters by Trend/Volume/SMC,
    and calls GoAPI ONLY for those that pass.
    """
    cache_key = "market_screener"
    cached = _cache_get(cache_key)
    if cached:
        return cached

    tickers = [f"{t}.JK" for t in LQ45]
    tickers_str = " ".join(tickers)

    # Batch download
    try:
        data_1d = yf.download(tickers_str, period="3mo", interval="1d", group_by="ticker", progress=False, auto_adjust=True)
        data_1h = yf.download(tickers_str, period="1mo", interval="1h", group_by="ticker", progress=False, auto_adjust=True)
    except Exception as e:
        return {"error": f"Batch download failed: {str(e)}"}

    results = []
    
    for t in LQ45:
        t_jk = f"{t}.JK"
        if t_jk not in data_1d.columns.get_level_values(0):
            continue

        # Extract ticker dataframe
        df_1d = data_1d[t_jk].dropna()
        df_1h = data_1h[t_jk].dropna()

        if len(df_1d) < 20 or len(df_1h) < 20:
            continue

        # Technical check
        tv = validate_trend_volume(df_1d)
        
        # SMC check 
        df_4h = df_1h.resample("4h").agg({
            "Open": "first", "High": "max", "Low": "min",
            "Close": "last", "Volume": "sum"
        }).dropna()
        
        smc_4h = extract_smc(df_4h, "4H")
        
        # Only process further if there's a structure
        if smc_4h.get("bias") == "Neutral":
            continue

        # To limit API calls drastically, let's say it must be in a bullish setup
        if smc_4h.get("bias") != "Bullish":
            continue
            
        # Is price near POI (within 3% of POI bounds)?
        curr_price = tv["current_price"]
        poi_low = smc_4h.get("poi_low", 0)
        poi_high = smc_4h.get("poi_high", 0)
        
        near_poi = False
        if poi_low > 0 and poi_high > 0:
            if curr_price <= (poi_high * 1.03) and curr_price >= (poi_low * 0.97):
                near_poi = True

        status_flags = []
        if tv["trend_valid"]: status_flags.append("Uptrend")
        if tv["vol_valid"]: status_flags.append("High Vol")
        if near_poi: status_flags.append("Near POI")
        
        # Must have at least uptrend or near_poi to be considered a 'candidate'
        if not (tv["trend_valid"] or near_poi):
            continue
            
        score = 0
        if near_poi: score += 40
        if tv["vol_valid"]: score += 30
        if tv["trend_valid"]: score += 30
        
        results.append({
            "ticker": t,
            "score": score,
            "price": curr_price,
            "flags": status_flags,
            "smc": smc_4h
        })

    # Sort by tech score
    results.sort(key=lambda x: x["score"], reverse=True)
    
    # We only call GoAPI for top 10 to preserve limits
    top_candidates = results[:10]
    final_results = []
    
    for cand in top_candidates:
        broker_data = fetch_broker_summary(cand["ticker"])
        flow = analyze_flow(broker_data)
        cand["flow"] = flow
        # Add a final score combining tech and flow
        flow_score = cand["flow"].get("acc_score", 0)
        if cand["flow"].get("eligible"): 
            cand["score"] += 20
            
        final_results.append(cand)
        
    final_results.sort(key=lambda x: x["score"], reverse=True)
    
    res = {"candidates": final_results}
    _cache_set(cache_key, res)
    return res
