from django.urls import path
from . import views

urlpatterns = [
    path('',                        views.index,      name='index'),
    path('api/ihsg/',               views.api_ihsg,   name='api_ihsg'),
    path('api/stock/<str:ticker>/', views.api_stock,  name='api_stock'),
    path('api/broker/<str:ticker>/',views.api_broker, name='api_broker'),
    path('api/smc/<str:ticker>/',   views.api_smc,    name='api_smc'),
    path('api/screener/',           views.api_screener,name='api_screener'),
]
