from django.urls import path
from . import views

app_name = 'dashboard'

urlpatterns = [
    path('',                        views.index,        name='index'),
    path('screener/',               views.screener,     name='screener'),
    path('stock/<str:ticker>/',     views.stock_detail, name='stock_detail'),
    path('backtest/',               views.backtest_page, name='backtest'),
    path('api/backtest/',           views.api_backtest,  name='api_backtest'),
    path('api/status/',             views.api_status,    name='api_status'),
]
