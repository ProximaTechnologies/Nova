from datetime import timedelta
from unittest.mock import Mock, patch

import pytest
from django.core.exceptions import ValidationError
from django.test import override_settings
from django.utils import timezone

from config.models import AlloyVersion, Node, NodeFeatures, SupervisorVersion, Tenant, normalize_alloy_version


def test_test_works():
    assert True


@pytest.mark.django_db
def test_supervisor_version_requires_semver_without_v_prefix():
    SupervisorVersion.objects.create(version="1.2.3-rc.1+build.4")

    with pytest.raises(ValidationError):
        SupervisorVersion.objects.create(version="v1.2.3")


def test_normalize_alloy_version_adds_v_to_stable_semver():
    assert normalize_alloy_version("1.11.0") == "v1.11.0"
    assert normalize_alloy_version("v1.11.0") == "v1.11.0"
    assert normalize_alloy_version("latest") == "latest"


@pytest.mark.django_db
def test_alloy_version_model_normalizes_v_prefix():
    version = AlloyVersion.objects.create(version="1.11.0")

    assert version.version == "v1.11.0"


@pytest.mark.django_db
@override_settings(UI_URL="https://nova.example.com/")
@patch("config.models.create_tenant")
@patch("config.models.set_json")
@patch("config.models.set_string")
def test_node_features_redis_payload_includes_healthcheck_url(
        mock_set_string,
        mock_set_json,
        _mock_create_tenant,
):
    tenant = Tenant.objects.create(name="Acme", grafana_org_id=1)
    node = Node.objects.create(tenant=tenant, node_name="collector-1", alloy_version="1.10.2")
    features = NodeFeatures(node=node, linuxMonitor=True)

    features._push_to_redis()

    assert mock_set_json.call_count == 2
    feature_key, payload = mock_set_json.call_args_list[0].args[:2]
    assert feature_key == f"nova:feat:{tenant.name}:{node.node_id.hex}"
    assert payload["linuxMonitor"] is True
    assert payload["alloyVersion"] == "v1.10.2"
    assert payload["Healthcheck_URL"] == "https://nova.example.com/agent/check-in"
    assert payload["observabilityTenantID"]

    credential_key, credentials = mock_set_json.call_args_list[1].args[:2]
    assert credential_key == f"nova:cred:{tenant.name}:{node.node_id.hex}"
    assert credentials == {}

    mock_set_string.assert_called_once()
    ver_key, version = mock_set_string.call_args.args[:2]
    assert ver_key == f"nova:ver:{tenant.name}:{node.node_id.hex}"
    assert int(version) > 10_000_000_000


@pytest.mark.django_db
@patch("config.models.create_tenant")
def test_node_connection_state_warns_after_two_minutes_and_fails_after_five(_mock_create_tenant):
    tenant = Tenant.objects.create(name="Acme", grafana_org_id=1)
    now = timezone.now()
    node = Node.objects.create(
        tenant=tenant,
        node_name="collector-1",
        last_seen_at=now - timedelta(seconds=30),
    )

    with patch("config.models.timezone.now", return_value=now):
        assert node.connection_state == "online"
        assert node.connection_status_label == "Online"
        assert node.connection_badge_class == "badge-success"
        assert node.is_online(now=now) is True

        node.last_seen_at = now - timedelta(minutes=2)
        assert node.connection_state == "warning"
        assert node.connection_status_label == "Warning"
        assert node.connection_badge_class == "badge-warning"
        assert node.is_online(now=now) is False

        node.last_seen_at = now - timedelta(minutes=5)
        assert node.connection_state == "warning"

        node.last_seen_at = now - timedelta(minutes=5, seconds=1)
        assert node.connection_state == "failed"
        assert node.connection_status_label == "Failed"
        assert node.connection_badge_class == "badge-error"


@pytest.mark.django_db
@patch("config.models.create_tenant")
def test_node_check_in_republishes_redis_payload_when_alloy_version_changes(_mock_create_tenant, monkeypatch):
    push = Mock()
    monkeypatch.setattr("config.models.NodeFeatures._push_to_redis", push)
    monkeypatch.setattr("config.models.Node._broadcast_dashboard_status", Mock())
    monkeypatch.setattr("config.models.transaction.on_commit", lambda callback: callback())

    tenant = Tenant.objects.create(name="Acme", grafana_org_id=1)
    node = Node.objects.create(tenant=tenant, node_name="collector-1")
    NodeFeatures.objects.create(node=node)
    push.reset_mock()

    node.mark_seen(alloy_version="v1.10.2")
    node.mark_seen(alloy_version="v1.10.2")
    node.mark_seen(alloy_version="1.11.0")

    assert push.call_count == 2
    assert node.alloy_version == "v1.11.0"
