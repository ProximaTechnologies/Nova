import asyncio

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncJsonWebsocketConsumer
from django.conf import settings
from django.http import Http404

from config.models import TenantUser
from .dashboard_live import dashboard_group_name, dashboard_live_payload
from .utils import get_tenant_from_secure_id


class DashboardConsumer(AsyncJsonWebsocketConsumer):
    async def connect(self):
        self.refresh_task = None
        self.tenant = None
        self.group_name = None

        user = self.scope["user"]
        if not user.is_authenticated:
            await self.close(code=4401)
            return

        tenant_id_secure = self.scope["url_route"]["kwargs"]["tenant_id_secure"]
        self.tenant = await self._get_user_tenant(user.pk, tenant_id_secure)
        if self.tenant is None:
            await self.close(code=4403)
            return

        self.group_name = dashboard_group_name(self.tenant.pk)
        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()
        await self.send_json(await self._payload())
        self.refresh_task = asyncio.create_task(self._refresh_loop())

    async def disconnect(self, close_code):
        if self.refresh_task:
            self.refresh_task.cancel()
        if self.group_name:
            await self.channel_layer.group_discard(self.group_name, self.channel_name)

    async def dashboard_status(self, event):
        await self.send_json(event["payload"])

    async def _refresh_loop(self):
        refresh_seconds = max(getattr(settings, "DASHBOARD_WEBSOCKET_REFRESH_SECONDS", 5), 1)
        try:
            while True:
                await asyncio.sleep(refresh_seconds)
                await self.send_json(await self._payload())
        except asyncio.CancelledError:
            return

    @database_sync_to_async
    def _get_user_tenant(self, user_id, tenant_id_secure):
        try:
            tenant = get_tenant_from_secure_id(tenant_id_secure)
        except Http404:
            return None
        if not TenantUser.objects.filter(user_id=user_id, tenant=tenant).exists():
            return None
        return tenant

    @database_sync_to_async
    def _payload(self):
        return dashboard_live_payload(self.tenant)
