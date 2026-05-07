from django.urls import path
from . import views

app_name = 'dashboard'

urlpatterns = [
    # Landing page (first impression — STIX brand)
    path('',                        views.landing_page,    name='landing'),
    # Main app pages
    path('dashboard/',              views.index,           name='index'),
    path('screener/',               views.screener,        name='screener'),
    path('stock/<str:ticker>/',     views.stock_detail,    name='stock_detail'),
    path('backtest/',               views.backtest_page,   name='backtest'),
    path('trade-log/',              views.trade_log_page,  name='trade_log'),
    # APIs
    path('api/backtest/',           views.api_backtest,    name='api_backtest'),
    path('api/backtest_dual/',      views.api_backtest_dual, name='api_backtest_dual'),
    path('api/status/',             views.api_status,      name='api_status'),
    path('api/trade-log/',          views.api_trade_log,   name='api_trade_log'),
]
