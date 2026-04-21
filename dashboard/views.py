import json
from django.shortcuts import render
from django.http import JsonResponse
from django.views.decorators.http import require_GET
from . import analysis as an

def index(request):
    """Renders the main dashboard shell HTML. All data is loaded async via JS."""
    return render(request, "index.html")

@require_GET
def api_ihsg(request):
    data = an.fetch_ihsg()
    return JsonResponse(data)

@require_GET
def api_stock(request, ticker):
    ticker_jk = f"{ticker.upper()}.JK"
    data = an.fetch_ohlcv(ticker_jk)
    return JsonResponse(data)

@require_GET
def api_broker(request, ticker):
    broker_data = an.fetch_broker_summary(ticker.upper())
    flow        = an.analyze_flow(broker_data)
    return JsonResponse({"broker": broker_data, "flow": flow})

@require_GET
def api_smc(request, ticker):
    ticker_jk = f"{ticker.upper()}.JK"
    ohlcv = an.fetch_ohlcv(ticker_jk)

    tv = an.validate_trend_volume(ohlcv["data_1d"])
    smc_4h = an.extract_smc(ohlcv["data_4h"], "4H")
    smc_1h = an.extract_smc(ohlcv["data_1h"], "1H")

    return JsonResponse({
        "trend_volume": tv,
        "smc_4h": smc_4h,
        "smc_1h": smc_1h,
    })

@require_GET
def api_screener(request):
    """Runs the market-wide screening algorithm."""
    data = an.screen_market()
    return JsonResponse(data)
