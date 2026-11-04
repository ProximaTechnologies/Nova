from django.urls import path

from .consumers import DashboardConsumer

websocket_urlpatterns = [
    path("ws/<slug:tenant_id_secure>/dashboard/", DashboardConsumer.as_asgi(), name="dashboard_ws"),
]
