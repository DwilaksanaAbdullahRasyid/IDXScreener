import json
import time
import datetime
from pathlib import Path
from django.shortcuts import render, get_object_or_404
from django.http import JsonResponse
from django.views.decorators.http import require_GET
from .models import Stock, ForeignFlow, SMCSignal
from . import analysis
from . import backtest as bt
from . import trade_log as tl

BASE_DIR = Path(__file__).resolve().parent.parent


def _get_cached_bt_stats():
    """
    Reads backtest_cache.json (if it exists and is fresh) and returns a
    minimal stats dict for the landing page hero/metrics section.
    Returns None if no cache file or cache is stale (> 48 h).
    """
    cache_file = BASE_DIR / "backtest_cache.json"
    try:
        if not cache_file.exists():
            return None
        if (time.time() - cache_file.stat().st_mtime) > 172_800:   # 48 h
            return None
        with open(cache_file) as f:
            data = json.load(f)
        m = data.get("metrics", {})
        p = data.get("params",  {})
        return {
            "win_rate":      round(m.get("win_rate",      0), 1),
            "total_trades":  m.get("total_trades",  0),
            "profit_factor": round(m.get("profit_factor", 0), 2),
            "avg_win_r":     round(m.get("avg_win_r",     0), 2),
            "period":        p.get("period",  "—"),
            "version":       p.get("version", "—"),
        }
    except Exception:
        return None


def _transform_backtest_metrics(metrics):
    """Transform backtest metrics keys to match template expectations."""
    if not metrics:
        return {}
    return {
        "trades": metrics.get("trades", 0),
        "wins": metrics.get("wins", 0),
        "losses": metrics.get("losses", 0),
        "win_rate": metrics.get("wr", 0),
        "total_r": metrics.get("total_r", 0),
        "avg_win_r": metrics.get("avg_win", 0),
        "avg_loss_r": metrics.get("avg_loss", 0),
        "profit_factor": metrics.get("pf", 0),
    }


def landing_page(request):
    """STIX landing page — first impression before the dashboard."""
    from dashboard.backtest_dual import run_dual_backtest
    from dashboard.strategy_config import format_strategy_summary, BACKTEST_BASELINE, NON_FCA_BASELINE, FCA_ADDITION

    ihsg = analysis.fetch_ihsg()
    screener_data = analysis.screen_market()
    summary = tl.get_summary_stats(days=30)
    recent_trades = tl.get_trade_log_history(days=7)[:5]  # last 5 signals

    # Fetch backtest metrics (uses cache, no re-run unless forced)
    try:
        bt_results = run_dual_backtest(force=False)
        bt_metrics = bt_results.get("metrics", {})
        combined_metrics = _transform_backtest_metrics(bt_metrics.get("combined", BACKTEST_BASELINE))
        non_fca_metrics = _transform_backtest_metrics(bt_metrics.get("non_fca", NON_FCA_BASELINE))
        fca_metrics = _transform_backtest_metrics(bt_metrics.get("fca", FCA_ADDITION))
    except Exception:
        combined_metrics = _transform_backtest_metrics(BACKTEST_BASELINE)
        non_fca_metrics = _transform_backtest_metrics(NON_FCA_BASELINE)
        fca_metrics = _transform_backtest_metrics(FCA_ADDITION)

    context = {
        "ihsg":           ihsg,
        "signal_count":   screener_data.get("total_candidates", 0),
        "universe_size":  screener_data.get("universe_size", 87),
        "summary":        summary,
        "recent_trades":  recent_trades,
        "today":          datetime.date.today(),
        "bt_stats":       _get_cached_bt_stats(),   # 48h cache for hero metrics

        # Backtest metrics breakdown
        "backtest_metrics": {
            "combined": combined_metrics,
            "non_fca": non_fca_metrics,
            "fca": fca_metrics,
        },

        # Strategy summary
        "strategy_summary": format_strategy_summary(),
    }
    return render(request, "dashboard/landing.html", context)


def index(request):
    """Homepage — IHSG status + top screener candidates."""
    ihsg = analysis.fetch_ihsg()
    screener_data = analysis.screen_market()
    candidates = screener_data.get("candidates", [])

    context = {
        "ihsg": ihsg,
        "candidates": candidates,
        "today": datetime.date.today(),
        "error": screener_data.get("error"),
        "api_calls_today": screener_data.get("api_calls_today", 0),
        "api_calls_remaining": screener_data.get("api_calls_remaining", 28),
        "universe_size": screener_data.get("universe_size", 0),
        "fca_excluded": screener_data.get("fca_excluded", 0),
        "trading_session": screener_data.get("trading_session", {}),
    }
    return render(request, "dashboard/index.html", context)


def screener(request):
    """Screener page — Daily V3.1 strategy signals: confirmed, watch, and caution buckets."""
    # Fetch market regime (IHSG condition)
    ihsg = analysis.fetch_ihsg()

    try:
        screener_data = analysis.screen_market()
    except Exception as e:
        import traceback
        log.error(f"Screener failed: {str(e)}\n{traceback.format_exc()}")
        screener_data = {
            "confirmed": [], "watch": [], "caution": [],
            "api_calls_remaining": 28, "api_calls_today": 0,
            "error": f"Screener load failed: {str(e)[:200]}"
        }

    confirmed = screener_data.get("confirmed", [])
    watch     = screener_data.get("watch",     [])
    caution   = screener_data.get("caution",   [])

    context = {
        # Market regime condition
        "ihsg": ihsg,
        # Error handling
        "error": screener_data.get("error"),
        # Signal buckets (confirmed, watch, caution)
        "confirmed":  confirmed,
        "watch":      watch,
        "caution":    caution,
        # Counts
        "total_confirmed": len(confirmed),
        "total_watch":     len(watch),
        "total_caution":   len(caution),
        # API meta
        "api_calls_remaining": screener_data.get("api_calls_remaining", 28),
        "api_calls_today":     screener_data.get("api_calls_today", 0),
    }
    return render(request, "dashboard/screener.html", context)


def stock_detail(request, ticker):
    """Detail page — OHLCV charts, SMC levels, and broker flow for one stock."""
    ticker = ticker.upper()
    ticker_jk = f"{ticker}.JK"

    ohlcv = analysis.fetch_ohlcv(ticker_jk)
    broker_raw = analysis.fetch_broker_summary(ticker)
    flow = analysis.analyze_flow(broker_raw)

    smc_4h = {}
    smc_1h = {}
    trend_vol = {}

    if ohlcv["data_4h"]:
        smc_4h = analysis.extract_smc(ohlcv["data_4h"], "4H")
    if ohlcv["data_1h"]:
        smc_1h = analysis.extract_smc(ohlcv["data_1h"], "1H")
    if ohlcv["data_1d"]:
        trend_vol = analysis.validate_trend_volume(ohlcv["data_1d"])

    broker_data = broker_raw.get("data", {})
    top_buy  = broker_data.get("buy",  [])[:10]
    top_sell = broker_data.get("sell", [])[:10]

    context = {
        "ticker": ticker,
        "ohlcv": ohlcv,
        "smc_4h": smc_4h,
        "smc_1h": smc_1h,
        "trend_vol": trend_vol,
        "flow": flow,
        "top_buy": top_buy,
        "top_sell": top_sell,
        "broker_source": broker_raw.get("source", "unknown"),
        "broker_error": broker_raw.get("error"),
    }
    return render(request, "dashboard/stock_detail.html", context)


def backtest_page(request):
    """Renders the backtest dashboard page."""
    return render(request, "dashboard/backtest.html")


@require_GET
def api_backtest(request):
    """
    Runs (or returns cached) walk-forward backtest.
    Pass ?force=1 to bypass cache and re-run from scratch.
    """
    try:
        force = request.GET.get("force", "0") == "1"
        data = bt.run_backtest(force=force)
        return JsonResponse(data)
    except Exception as e:
        import traceback
        return JsonResponse({
            "error": str(e),
            "traceback": traceback.format_exc()
        }, status=500)


@require_GET
def api_backtest_dual(request):
    """
    Runs (or returns cached) DUAL backtest: Non-FCA baseline + FCA expansion.
    Combined metrics showing all 142 trades.
    Pass ?force=1 to bypass cache and re-run from scratch.
    """
    try:
        from dashboard.backtest_dual import run_dual_backtest
        force = request.GET.get("force", "0") == "1"
        result = run_dual_backtest(force=force)

        # Transform result to match backtest.py format for dashboard
        m = result["metrics"]["combined"]
        trades = result["trades"]["combined"]

        # Format for dashboard display
        data = {
            "trades": trades,
            "metrics": {
                "total_trades": m["trades"],
                "wins": m["wins"],
                "losses": m["losses"],
                "win_rate": m["wr"],
                "total_r": m["total_r"],
                "avg_win_r": m["avg_win"],
                "avg_loss_r": m["avg_loss"],
                "profit_factor": m.get("pf", 0),
                "breakdown": {
                    "non_fca": _transform_backtest_metrics(result["metrics"]["non_fca"]),
                    "fca": _transform_backtest_metrics(result["metrics"]["fca"]),
                },
            }
        }
        return JsonResponse(data)
    except Exception as e:
        import traceback
        return JsonResponse({
            "error": str(e),
            "traceback": traceback.format_exc()
        }, status=500)


@require_GET
def api_status(request):
    """Returns GoAPI daily quota usage and broker cache summary."""
    return JsonResponse(analysis.get_api_status())


def trade_log_page(request):
    """Trade Log page — daily signal tracking with live SL/TP status."""
    date_str = request.GET.get("date", datetime.date.today().isoformat())
    trades   = tl.update_trade_statuses(date_str)
    history  = tl.get_trade_log_history(days=30)
    summary  = tl.get_summary_stats(days=30)

    context = {
        "trades":    trades,
        "history":   history,
        "summary":   summary,
        "date":      date_str,
        "today":     datetime.date.today().isoformat(),
    }
    return render(request, "dashboard/trade_log.html", context)


@require_GET
def api_trade_log(request):
    """Returns trade log entries for a given date as JSON."""
    date_str = request.GET.get("date", datetime.date.today().isoformat())
    return JsonResponse({"trades": tl.update_trade_statuses(date_str)})
