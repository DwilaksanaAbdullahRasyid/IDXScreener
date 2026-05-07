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

    Each entry includes backtest TP/SL levels so trades follow backtest strategy:
      - Entry price at POI zone
      - TP1/TP2/TP3: partial exits at 1R, 2R, 3R
      - SL: stop loss below POI
      - No EOD exit — trades held until TP/SL hit or MAX_HOLD exceeded

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

        composite = cand.get("composite", {}) or {}

        # Get backtest entry levels (from screener if available)
        bt_entry = cand.get("backtest_entry", {})

        # Fallback: compute from POI if backtest_entry not provided
        if not bt_entry:
            from .strategy_config import TP1_R, TP2_R, TP3_R, SL_FACTOR
            entry_price = cand.get("price") or cand.get("close") or 0
            poi_low = cand.get("poi_low") or cand.get("poi_4h", {}).get("low", 0)
            poi_high = cand.get("poi_high") or cand.get("poi_4h", {}).get("high", 0)

            if entry_price > 0 and poi_low > 0:
                risk = entry_price - (poi_low * (1 - SL_FACTOR))
                bt_entry = {
                    "entry_price": entry_price,
                    "tp1": entry_price + risk * TP1_R,
                    "tp2": entry_price + risk * TP2_R,
                    "tp3": entry_price + risk * TP3_R,
                    "sl": poi_low * (1 - SL_FACTOR),
                    "risk_amount": risk,
                }

        entry = {
            "ticker":            ticker,
            "signal_date":       date_str,
            "logged_at":         now_str,
            # Prices — backtest TP/SL levels (replaces single TP)
            "entry_price":       bt_entry.get("entry_price") or cand.get("price") or cand.get("close"),
            "sl":                bt_entry.get("sl"),
            "tp1":               bt_entry.get("tp1"),
            "tp2":               bt_entry.get("tp2"),
            "tp3":               bt_entry.get("tp3"),
            "tp":                bt_entry.get("tp3"),  # fallback for legacy code
            "poi_low":           cand.get("poi_low") or cand.get("poi_4h", {}).get("low"),
            "poi_high":          cand.get("poi_high") or cand.get("poi_4h", {}).get("high"),
            "risk_pct":          cand.get("risk_pct"),
            # V3.9 Grade + Confluence (aligned with backtest grading system)
            "grade":             composite.get("grade", "D"),
            "confluence_score":  composite.get("confluence_score"),
            # Backtest partial exit tracking
            "tp1_hit":           False,
            "tp2_hit":           False,
            "tp3_hit":           False,
            "composite_score":   composite.get("composite_score"),
            "flow_confirmed":    bool(cand.get("flow", {}).get("eligible")) if isinstance(cand.get("flow"), dict) else False,
            # Signal context
            "flags":             cand.get("flags", []),
            "flow_signal":       cand.get("flow", {}).get("signal", "") if isinstance(cand.get("flow"), dict) else "",
            "flow_quality":      cand.get("flow_quality"),
            "bias":              cand.get("bias", ""),
            "score":             cand.get("score"),
            # Status
            "status":            "PENDING",
            "updated_at":        now_str,
            "exit_price":        None,
            "pnl_r":             None,
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

        # Backtest strategy: TP1/TP2/TP3 for partial exits
        tp1    = entry.get("tp1") or 0
        tp2    = entry.get("tp2") or 0
        tp3    = entry.get("tp3") or 0
        tp     = entry.get("tp") or tp3  # fallback for legacy code

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

        # OPEN → track partial exits (TP1/TP2/TP3) or SL
        if entry["status"] == "OPEN":
            # Track which TPs have been hit (for partial exit reporting)
            if tp1 > 0 and day_high >= tp1 and not entry.get("tp1_hit"):
                entry["tp1_hit"] = True
                entry["updated_at"] = now_str
                changed = True

            if tp2 > 0 and day_high >= tp2 and not entry.get("tp2_hit"):
                entry["tp2_hit"] = True
                entry["updated_at"] = now_str
                changed = True

            if tp3 > 0 and day_high >= tp3 and not entry.get("tp3_hit"):
                entry["tp3_hit"] = True
                entry["status"] = "HIT_TP"
                entry["exit_price"] = tp3
                # Calculate P&L based on partial exits
                # Assume 1/3 at TP1, 1/3 at TP2, 1/3 at TP3
                if sl > 0:
                    risk = ep - sl
                    pnl = (
                        ((tp1 - ep) / risk) * (1/3) +  # 1/3 at TP1
                        ((tp2 - ep) / risk) * (1/3) +  # 1/3 at TP2
                        ((tp3 - ep) / risk) * (1/3)    # 1/3 at TP3
                    )
                    entry["pnl_r"] = round(pnl, 3)
                else:
                    entry["pnl_r"] = round((tp3 - ep) / max(ep, 0.0001), 3)
                entry["updated_at"] = now_str
                changed = True

            # Check for SL hit (stops out entire remaining position)
            elif sl > 0 and day_low <= sl:
                # If TP1/TP2 already hit, calculate partial P&L; otherwise full loss
                if entry.get("tp2_hit"):
                    # TP1 + TP2 hit, only 1/3 remaining at risk
                    risk = ep - sl
                    pnl = (
                        ((tp1 - ep) / risk) * (1/3) +  # 1/3 at TP1
                        ((tp2 - ep) / risk) * (1/3) +  # 1/3 at TP2
                        (sl - ep) / risk * (1/3)       # 1/3 stopped at SL
                    )
                    entry["pnl_r"] = round(pnl, 3)
                elif entry.get("tp1_hit"):
                    # TP1 hit, 2/3 remaining at risk
                    risk = ep - sl
                    pnl = (
                        ((tp1 - ep) / risk) * (1/3) +  # 1/3 at TP1
                        (sl - ep) / risk * (2/3)       # 2/3 stopped at SL
                    )
                    entry["pnl_r"] = round(pnl, 3)
                else:
                    # No exits hit, full position stopped at SL
                    entry["pnl_r"] = -1.0

                entry["status"] = "HIT_SL"
                entry["exit_price"] = sl
                entry["updated_at"] = now_str
                changed = True

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
