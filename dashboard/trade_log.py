"""
trade_log.py — Daily signal persistence and live trade status tracking for STIX.

Each trading day gets its own file: trade_logs/YYYY-MM-DD.json
Running the screener multiple times on the same day merges new signals in
without overwriting the status of existing entries.

Status lifecycle:
    PENDING  → signal detected, waiting for price to enter POI zone
    OPEN     → price entered POI zone (entry triggered)
    HIT_TP   → take-profit level crossed ✅
    HIT_SL   → stop-loss level crossed ❌
    EXPIRED  → end of session, neither SL nor TP was hit ⏰
"""

import json
import datetime
import time
from pathlib import Path

try:
    import yfinance as yf
    _YF_AVAILABLE = True
except ImportError:
    _YF_AVAILABLE = False

BASE_DIR = Path(__file__).resolve().parent.parent
LOG_DIR  = BASE_DIR / "trade_logs"

# In-memory cache for update_trade_statuses() to avoid re-fetching on every
# page refresh. Keyed by date_str → (timestamp, trades_list)
_status_cache: dict = {}
STATUS_CACHE_TTL = 300  # 5 minutes


def _log_path(date_str: str) -> Path:
    LOG_DIR.mkdir(exist_ok=True)
    return LOG_DIR / f"{date_str}.json"


def _load_log(date_str: str) -> list:
    """Read the log file for a given date. Returns [] if missing or corrupt."""
    path = _log_path(date_str)
    if not path.exists():
        return []
    try:
        with open(path, "r") as f:
            return json.load(f)
    except Exception:
        return []


def _save_log(date_str: str, entries: list):
    """Persist the entries list to today's log file."""
    try:
        with open(_log_path(date_str), "w") as f:
            json.dump(entries, f, indent=2)
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def save_daily_signals(candidates: list, date_str: str | None = None) -> int:
    """
    Persist today's confirmed screener candidates to the daily log file.
    Tickers already present in the file keep their existing status (no overwrite).
    Returns the number of NEW entries added.
    """
    if date_str is None:
        date_str = datetime.date.today().isoformat()

    existing = _load_log(date_str)
    existing_tickers = {e["ticker"] for e in existing}

    now_str = datetime.datetime.now().isoformat(timespec="seconds")
    added   = 0

    for cand in candidates:
        ticker = cand.get("ticker") or cand.get("symbol", "")
        if not ticker or ticker in existing_tickers:
            continue  # already tracked — don't overwrite status

        entry = {
            "ticker":       ticker,
            "signal_date":  date_str,
            "logged_at":    now_str,
            # Prices
            "entry_price":  cand.get("entry_price") or cand.get("price") or cand.get("close"),
            "sl":           cand.get("sl") or cand.get("stop_loss"),
            "tp":           cand.get("tp") or cand.get("take_profit"),
            "poi_low":      cand.get("poi_low"),
            "poi_high":     cand.get("poi_high"),
            "risk_pct":     cand.get("risk_pct"),
            # Signal context
            "flags":        cand.get("flags", []),
            "flow_signal":  cand.get("flow", {}).get("signal", "") if isinstance(cand.get("flow"), dict) else "",
            "flow_quality": cand.get("flow_quality"),
            "bias":         cand.get("bias", ""),
            "score":        cand.get("score"),
            # Status
            "status":       "PENDING",
            "updated_at":   now_str,
            "exit_price":   None,
            "pnl_r":        None,
        }
        existing.append(entry)
        existing_tickers.add(ticker)
        added += 1

    if added > 0:
        _save_log(date_str, existing)

    return added


def load_daily_log(date_str: str | None = None) -> list:
    """Return the trade log entries for the given date (today by default)."""
    if date_str is None:
        date_str = datetime.date.today().isoformat()
    return _load_log(date_str)


def update_trade_statuses(date_str: str | None = None) -> list:
    """
    Fetch intraday prices and update PENDING/OPEN entries to HIT_TP, HIT_SL,
    or EXPIRED based on today's high/low range.

    Results are cached in-memory for 5 minutes to prevent hammering yfinance
    on rapid page refreshes.
    """
    if date_str is None:
        date_str = datetime.date.today().isoformat()

    # Serve from in-memory cache if fresh
    cached = _status_cache.get(date_str)
    if cached and (time.time() - cached[0]) < STATUS_CACHE_TTL:
        return cached[1]

    entries = _load_log(date_str)
    if not entries:
        return entries

    # Find tickers that still need price checks
    live_tickers = [
        e["ticker"] for e in entries
        if e["status"] in ("PENDING", "OPEN") and e.get("entry_price")
    ]

    price_data: dict = {}  # ticker → {"high": float, "low": float, "last": float}

    if live_tickers and _YF_AVAILABLE:
        try:
            tickers_jk = [f"{t}.JK" for t in live_tickers]
            raw = yf.download(
                " ".join(tickers_jk),
                period="1d", interval="5m",
                group_by="ticker", progress=False, auto_adjust=True,
            )
            import pandas as pd
            for t, t_jk in zip(live_tickers, tickers_jk):
                try:
                    if isinstance(raw.columns, pd.MultiIndex):
                        df = raw[t_jk].dropna()
                    else:
                        df = raw.dropna()
                    if df.empty:
                        continue
                    price_data[t] = {
                        "high": float(df["High"].max()),
                        "low":  float(df["Low"].min()),
                        "last": float(df["Close"].iloc[-1]),
                    }
                except Exception:
                    pass
        except Exception:
            pass

    now_str  = datetime.datetime.now().isoformat(timespec="seconds")
    changed  = False

    for entry in entries:
        if entry["status"] not in ("PENDING", "OPEN"):
            continue

        t      = entry["ticker"]
        ep     = entry.get("entry_price") or 0
        sl     = entry.get("sl") or 0
        tp     = entry.get("tp") or 0
        poi_lo = entry.get("poi_low") or 0
        poi_hi = entry.get("poi_high") or 0

        pd_t = price_data.get(t)
        if not pd_t or not ep:
            continue

        day_high = pd_t["high"]
        day_low  = pd_t["low"]
        last     = pd_t["last"]

        # PENDING → OPEN: price touched the POI zone
        if entry["status"] == "PENDING":
            in_zone = poi_lo > 0 and poi_hi > 0 and day_low <= poi_hi and day_high >= poi_lo
            if in_zone or abs(last - ep) / ep < 0.01:  # within 1% of entry
                entry["status"]     = "OPEN"
                entry["updated_at"] = now_str
                changed             = True

        # OPEN → HIT_TP or HIT_SL
        if entry["status"] == "OPEN":
            if tp > 0 and day_high >= tp:
                entry["status"]     = "HIT_TP"
                entry["exit_price"] = tp
                entry["pnl_r"]      = round((tp - ep) / max(ep - sl, 0.0001), 3) if sl > 0 else None
                entry["updated_at"] = now_str
                changed             = True
            elif sl > 0 and day_low <= sl:
                entry["status"]     = "HIT_SL"
                entry["exit_price"] = sl
                entry["pnl_r"]      = -1.0
                entry["updated_at"] = now_str
                changed             = True

    # Mark remaining OPEN trades as EXPIRED if market is closed (after 16:05 WIB)
    now_local = datetime.datetime.now()
    market_closed = now_local.hour > 16 or (now_local.hour == 16 and now_local.minute >= 5)
    if market_closed:
        for entry in entries:
            if entry["status"] in ("PENDING", "OPEN"):
                entry["status"]     = "EXPIRED"
                entry["updated_at"] = now_str
                changed             = True

    if changed:
        _save_log(date_str, entries)

    _status_cache[date_str] = (time.time(), entries)
    return entries


def get_trade_log_history(days: int = 30) -> list:
    """
    Return all entries from the last `days` trading days, newest first.
    Passes each day through update_trade_statuses() for today only.
    """
    today      = datetime.date.today()
    all_trades = []

    for delta in range(days):
        date_str = (today - datetime.timedelta(days=delta)).isoformat()
        if delta == 0:
            # For today, run the status updater
            entries = update_trade_statuses(date_str)
        else:
            entries = _load_log(date_str)
        all_trades.extend(entries)

    return all_trades


def get_summary_stats(days: int = 30) -> dict:
    """
    Returns aggregate win/loss/pending stats for the last N days.
    Used for the landing page stats widget.
    """
    history = get_trade_log_history(days)
    total   = len(history)
    wins    = sum(1 for e in history if e["status"] == "HIT_TP")
    losses  = sum(1 for e in history if e["status"] == "HIT_SL")
    pending = sum(1 for e in history if e["status"] in ("PENDING", "OPEN"))
    expired = sum(1 for e in history if e["status"] == "EXPIRED")

    win_rate = round(wins / (wins + losses) * 100, 1) if (wins + losses) > 0 else 0
    avg_pnl  = round(
        sum(e["pnl_r"] for e in history if e.get("pnl_r") is not None)
        / max(wins + losses, 1), 3
    )

    return {
        "total":    total,
        "wins":     wins,
        "losses":   losses,
        "pending":  pending,
        "expired":  expired,
        "win_rate": win_rate,
        "avg_pnl_r": avg_pnl,
        "days":     days,
    }
