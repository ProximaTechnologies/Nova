import base64
from unittest.mock import Mock

import pytest
from django.urls import reverse

from agent.models import CollectorAuth
from config.models import Node, NodeFeatures, Tenant


@pytest.fixture(autouse=True)
def external_side_effects(monkeypatch):
    monkeypatch.setattr("config.models.create_tenant", Mock())
    monkeypatch.setattr("config.models.delete_tenant", Mock())
    monkeypatch.setattr("config.models.update_grafana_org_mapping", Mock())
    monkeypatch.setattr("config.models.logout_user_from_grafana_using_email", Mock())
    monkeypatch.setattr("config.models.Node._broadcast_dashboard_status", Mock())
    monkeypatch.setattr("config.models.NodeFeatures._push_to_redis", Mock())


@pytest.mark.django_db
def test_agent_check_in_marks_node_seen_and_records_alloy_version(client):
    tenant = Tenant.objects.create(name="Acme", grafana_org_id=1)
    node = Node.objects.create(tenant=tenant, node_name="collector-1")
    auth = CollectorAuth.objects.create(
        node=node,
        mimir_consumer_name=f"{node.node_id.hex}-mimir",
        mimir_password="secret-mimir",
    )
    credentials = f"{auth.mimir_consumer_name}:{auth.mimir_password}".encode("utf-8")

    response = client.post(
        reverse("agent_check_in", args=[node.node_id.hex, "v1.10.2"]),
        HTTP_AUTHORIZATION="Basic " + base64.b64encode(credentials).decode("ascii"),
    )

    assert response.status_code == 200
    node.refresh_from_db()
    assert node.last_seen_at is not None
    assert node.alloy_version == "v1.10.2"
    assert response.json()["node_id"] == node.node_id.hex


@pytest.mark.django_db
def test_agent_check_in_rejects_unknown_node(client):
    response = client.post(reverse("agent_check_in", args=["not-a-node", "v1.10.2"]))

    assert response.status_code == 404


@pytest.mark.django_db
def test_agent_config_refresh_republishes_node_features(client, settings, monkeypatch):
    settings.NOVACFG_CONFIG_REFRESH_TOKEN = "refresh-secret"
    push = Mock()
    monkeypatch.setattr("config.models.NodeFeatures._push_to_redis", push)

    tenant = Tenant.objects.create(name="Acme", grafana_org_id=1)
    node = Node.objects.create(tenant=tenant, node_name="collector-1")
    NodeFeatures.objects.create(node=node)
    push.reset_mock()

    response = client.post(
        reverse("agent_config_refresh"),
        data={"tenant_id": tenant.name, "node_id": node.node_id.hex},
        content_type="application/json",
        HTTP_AUTHORIZATION="Bearer refresh-secret",
    )

    assert response.status_code == 200
    assert response.json()["node_id"] == node.node_id.hex
    push.assert_called_once()


@pytest.mark.django_db
def test_agent_config_refresh_requires_bearer_token(client, settings):
    settings.NOVACFG_CONFIG_REFRESH_TOKEN = "refresh-secret"

    response = client.post(reverse("agent_config_refresh"), data={}, content_type="application/json")

    assert response.status_code == 401
