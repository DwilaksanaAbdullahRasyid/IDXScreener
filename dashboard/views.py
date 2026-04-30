import json
import datetime
from django.shortcuts import render, get_object_or_404
from django.http import JsonResponse
from django.views.decorators.http import require_GET
from .models import Stock, ForeignFlow, SMCSignal
from . import analysis
from . import backtest as bt


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
    """Screener page — IDX signal filter + full universe watchlist."""
    screener_data = analysis.screen_market()

    confirmed = screener_data.get("confirmed", [])
    watch     = screener_data.get("watch",     [])
    caution   = screener_data.get("caution",   [])
    watchlist = screener_data.get("watchlist", [])

    context = {
        # Signal buckets
        "confirmed":  confirmed,
        "watch":      watch,
        "caution":    caution,
        # Full watchlist
        "watchlist":  watchlist,
        # Counts
        "total_confirmed": len(confirmed),
        "total_watch":     len(watch),
        "total_caution":   len(caution),
        "total_watchlist": len(watchlist),
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
    force = request.GET.get("force", "0") == "1"
    data = bt.run_backtest(force=force)
    return JsonResponse(data)


@require_GET
def api_status(request):
    """Returns GoAPI daily quota usage and broker cache summary."""
    return JsonResponse(analysis.get_api_status())
