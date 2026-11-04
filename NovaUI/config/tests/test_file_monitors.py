from unittest.mock import Mock

import pytest
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError

from config.file_monitors import active_file_monitor_payload, sync_file_monitors, validate_file_monitor_specs
from config.models import (
    AuditLog,
    FileMonitor,
    Node,
    NodeFeatures,
    Tenant,
    TenantBilling,
    TenantLimit,
    TenantUsage,
)


@pytest.fixture(autouse=True)
def external_side_effects(monkeypatch):
    monkeypatch.setattr("config.models.create_tenant", Mock())
    monkeypatch.setattr("config.models.delete_tenant", Mock())
    monkeypatch.setattr("config.models.update_grafana_org_mapping", Mock())
    monkeypatch.setattr("config.models.logout_user_from_grafana_using_email", Mock())
    monkeypatch.setattr("config.models.set_json", Mock())
    monkeypatch.setattr("config.models.set_string", Mock())


@pytest.fixture
def tenant(db):
    plan = TenantBilling.objects.create(name="Starter", price_per_node=400)
    tenant = Tenant.objects.create(name="file-monitor-tenant", random_prefix="fmt", grafana_org_id=101)
    TenantLimit.objects.create(tenant=tenant, billing=plan, max_nodes=5)
    TenantUsage.objects.create(tenant=tenant)
    return tenant


@pytest.fixture
def node(tenant):
    node = Node.objects.create(tenant=tenant, node_name="file-node")
    NodeFeatures.objects.create(node=node, linuxMonitor=True, fileMonitor=True)
    return node


@pytest.mark.django_db
def test_file_monitor_validation_blocks_bad_paths_and_duplicates(tenant):
    with pytest.raises(ValidationError) as relative:
        validate_file_monitor_specs(tenant=tenant, specs=[{"file_path": "var/log/app.log", "enabled": True}])
    assert "absolute Linux path" in str(relative.value)

    with pytest.raises(ValidationError) as sensitive:
        validate_file_monitor_specs(tenant=tenant, specs=[{"file_path": "/etc/shadow", "enabled": True}])
    assert "private credentials" in str(sensitive.value)

    with pytest.raises(ValidationError) as duplicate:
        validate_file_monitor_specs(
            tenant=tenant,
            specs=[
                {"file_path": "/var/log/app.log", "enabled": True},
                {"file_path": "/var/log/../log/app.log", "enabled": True},
            ],
        )
    assert "duplicate path" in str(duplicate.value)


@pytest.mark.django_db
def test_file_monitor_validation_enforces_plan_limit(tenant):
    with pytest.raises(ValidationError) as exc:
        validate_file_monitor_specs(
            tenant=tenant,
            specs=[
                {"file_path": "/var/log/one.log", "enabled": True},
                {"file_path": "/var/log/two.log", "enabled": True},
                {"file_path": "/var/log/three.log", "enabled": True},
                {"file_path": "/var/log/four.log", "enabled": True},
            ],
        )

    assert "allows up to 3 file monitor path" in str(exc.value)


@pytest.mark.django_db
def test_sync_file_monitors_creates_updates_deletes_and_records_audit(node):
    actor = User.objects.create_user(username="file-monitor-admin", password="password")

    sync_file_monitors(
        node,
        [
            {"label": "App", "file_path": "/var/log/app.log", "enabled": True},
            {"label": "Audit", "file_path": "/var/log/audit.log", "enabled": False},
        ],
        actor=actor,
        require_enabled=True,
    )

    monitors = list(node.file_monitors.order_by("file_path"))
    assert len(monitors) == 2
    assert monitors[0].label == "App"
    assert monitors[0].enabled is True
    assert monitors[1].enabled is False
    assert active_file_monitor_payload(node) == [{"label": "App", "path": "/var/log/app.log"}]
    assert AuditLog.objects.filter(action="file_monitor.synced", actor=actor, tenant=node.tenant).exists()

    sync_file_monitors(
        node,
        [
            {
                "id": monitors[0].pk,
                "label": "Application",
                "file_path": "/var/log/application.log",
                "enabled": True,
            },
        ],
    )

    assert list(FileMonitor.objects.filter(node=node).order_by("file_path").values_list("file_path", flat=True)) == [
        "/var/log/application.log"
    ]


@pytest.mark.django_db
def test_node_feature_redis_payload_includes_only_enabled_file_monitors(node, monkeypatch):
    FileMonitor.objects.create(node=node, label="App", file_path="/var/log/app.log", enabled=True)
    FileMonitor.objects.create(node=node, label="Disabled", file_path="/var/log/disabled.log", enabled=False)
    payloads = []
    monkeypatch.setattr("config.models.set_json", lambda key, value, ttl=None: payloads.append(value))

    node.features._push_to_redis()

    assert payloads[0]["fileMonitor"] is True
    assert payloads[0]["fileMonitors"] == [{"label": "App", "path": "/var/log/app.log"}]
