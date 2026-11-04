import asyncio
from types import SimpleNamespace

import pytest
from channels.routing import URLRouter
from channels.testing import WebsocketCommunicator
from django.urls import path

from ui.consumers import DashboardConsumer
from ui.dashboard_live import dashboard_live_payload

pytestmark = pytest.mark.django_db(transaction=True)


def make_communicator(user, tenant_id_secure, consumer_cls=DashboardConsumer):
    class StaticUserMiddleware:
        def __init__(self, app):
            self.app = app

        async def __call__(self, scope, receive, send):
            scope["user"] = user
            return await self.app(scope, receive, send)

    return WebsocketCommunicator(
        StaticUserMiddleware(URLRouter([
            path("ws/<slug:tenant_id_secure>/dashboard/", consumer_cls.as_asgi()),
        ])),
        f"/ws/{tenant_id_secure}/dashboard/",
    )


def test_dashboard_websocket_auth_and_initial_payload(
        tenant_factory,
        node_factory,
        secure_tenant_id,
):
    scoped_tenant = tenant_factory("ws-member", prefix="wsm")
    node = node_factory(scoped_tenant, "ws-node")
    initial_payload = dashboard_live_payload(scoped_tenant)

    class NonMemberDashboardConsumer(DashboardConsumer):
        async def _get_user_tenant(self, user_id, tenant_id_secure):
            return None

    class TenantDashboardConsumer(DashboardConsumer):
        async def _get_user_tenant(self, user_id, tenant_id_secure):
            return scoped_tenant

        async def _payload(self):
            return initial_payload

    async def run():
        member = make_communicator(
            SimpleNamespace(is_authenticated=True, pk=1000),
            secure_tenant_id(scoped_tenant),
            TenantDashboardConsumer,
        )
        member_connected, _ = await member.connect(timeout=5)
        assert member_connected is True

        payload = await member.receive_json_from(timeout=5)
        assert payload["type"] == "dashboard.status"
        assert payload["nodes_never_connected"] == 1
        assert payload["collectors"][0]["node_id"] == str(node.node_id)

        await member.disconnect()
        member.stop(exceptions=False)

        non_member = make_communicator(
            SimpleNamespace(is_authenticated=True, pk=999),
            secure_tenant_id(scoped_tenant),
            NonMemberDashboardConsumer,
        )
        non_member_connected, non_member_code = await non_member.connect(timeout=5)
        assert non_member_connected is False
        assert non_member_code == 4403
        await non_member.wait(timeout=5)
        non_member.stop(exceptions=False)

    asyncio.run(run())
