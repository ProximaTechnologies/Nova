from datetime import timedelta
from unittest.mock import Mock

import pytest
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone

from config.file_monitors import active_file_monitor_payload
from config.models import (
    AlloyVersion,
    AlloyVersionArtifact,
    Node,
    NodeFeatures,
    NodeStatus,
    SupervisorVersion,
    SupervisorVersionArtifact,
    Tenant,
    TenantBilling,
    TenantBillingStatus,
    TenantLimit,
    TenantRole,
    TenantUsage,
    TenantUser,
)
from ui.dashboard_live import dashboard_live_payload
from ui.routing import websocket_urlpatterns


@pytest.fixture(autouse=True)
def external_side_effects(monkeypatch):
    monkeypatch.setattr("config.models.create_tenant", Mock())
    monkeypatch.setattr("config.models.delete_tenant", Mock())
    monkeypatch.setattr("config.models.update_grafana_org_mapping", Mock())
    monkeypatch.setattr("config.models.logout_user_from_grafana_using_email", Mock())
    monkeypatch.setattr("config.models.NodeFeatures._push_to_redis", Mock())


@pytest.fixture
def billing_plan(db):
    return TenantBilling.objects.create(name="Default", price_per_node=400)


def make_user(username):
    return User.objects.create_user(username=username, email=f"{username}@example.com", password="password")


def make_tenant(name, prefix, billing_plan):
    tenant = Tenant.objects.create(
        name=name,
        random_prefix=prefix,
        grafana_org_id=42,
        billing_status=TenantBillingStatus.ACTIVE,
    )
    TenantLimit.objects.create(
        tenant=tenant,
        billing=billing_plan,
        max_nodes=10,
        max_clusters=2,
        max_vcpus=16,
    )
    TenantUsage.objects.create(tenant=tenant, nodes=5, clusters=1, vcpus=4)
    return tenant


def make_node(tenant, name, **kwargs):
    node = Node.objects.create(tenant=tenant, node_name=name, **kwargs)
    NodeFeatures.objects.create(node=node, linuxMonitor=True)
    return node


def secure_id(tenant):
    return f"{tenant.random_prefix}-{tenant.name}"


@pytest.mark.django_db
def test_dashboard_renders_relevant_tenant_metrics_and_grafana_link(client, settings, billing_plan):
    settings.BILLING_ENABLED = True
    settings.GRAFANA_URL = "http://grafana.monitoring.svc.cluster.local:3000"
    settings.GRAFANA_EXTERNAL_URL = "https://grafana.example"
    user = make_user("dashboard-user")
    tenant = make_tenant("tenant-dashboard", "td", billing_plan)
    TenantUser.objects.create(user=user, tenant=tenant, role=TenantRole.ADMIN)
    now = timezone.now()
    make_node(tenant, "online", last_config_poll_at=now - timedelta(minutes=1))
    make_node(tenant, "warning", last_config_poll_at=now - timedelta(minutes=10))
    make_node(tenant, "offline", last_config_poll_at=now - timedelta(minutes=31))
    make_node(tenant, "new")
    make_node(tenant, "disabled", enabled=False)

    client.force_login(user)
    response = client.get(reverse("dashboard", args=[secure_id(tenant)]))

    assert response.status_code == 200
    assert response.context["grafana_url"] == "https://grafana.example/?orgId=42"
    assert response.context["status_counts"][NodeStatus.ONLINE] == 1
    assert response.context["status_counts"][NodeStatus.WARNING] == 1
    assert response.context["status_counts"][NodeStatus.OFFLINE] == 1
    assert response.context["status_counts"][NodeStatus.NEVER_CONNECTED] == 1
    assert response.context["status_counts"][NodeStatus.DISABLED] == 1
    assert response.context["resource_usage"][0]["percent"] == 50
    assert response.context["can_provision_collectors"] is True
    html = response.content.decode()
    assert 'href="https://grafana.example/?orgId=42"' in html
    assert 'target="_blank"' in html
    assert 'data-dashboard-live-url="/ws/td-tenant-dashboard/dashboard/"' in html
    assert "Add collector" in html
    assert "Billing" in html
    assert "Collector health" in html
    assert "Feature coverage" in html


@pytest.mark.django_db
def test_non_staff_tenant_admin_can_self_provision_collectors(client, billing_plan, monkeypatch):
    monkeypatch.setattr("ui.views._sync_apisix_consumers_after_commit", Mock())
    user = make_user("collector-self-service-user")
    tenant = make_tenant("tenant-self-service", "tss", billing_plan)
    TenantUser.objects.create(user=user, tenant=tenant, role=TenantRole.ADMIN)
    url = reverse("add_collector", args=[secure_id(tenant)])

    client.force_login(user)

    response = client.get(reverse("list_collectors", args=[secure_id(tenant)]))
    assert response.status_code == 200
    assert response.context["can_provision_collectors"] is True
    assert b"Add collector" in response.content

    response = client.get(url)
    assert response.status_code == 200

    response = client.post(url, {"platform": "linux", "node_name": "self-added", "linuxMonitor": "on"})
    assert response.status_code == 302
    node = Node.objects.get(tenant=tenant, node_name="self-added")
    assert response.url == reverse("run_script_collector", args=[secure_id(tenant), node.node_id])
    assert client.get(response.url).status_code == 200


@pytest.mark.django_db
def test_editor_adds_enabled_collector_by_default(client, billing_plan, monkeypatch):
    monkeypatch.setattr("ui.views._sync_apisix_consumers_after_commit", Mock())
    user = make_user("collector-add-user")
    tenant = make_tenant("tenant-add", "ta", billing_plan)
    TenantUser.objects.create(user=user, tenant=tenant, role=TenantRole.EDITOR)

    client.force_login(user)
    response = client.post(
        reverse("add_collector", args=[secure_id(tenant)]),
        {
            "node_name": "collector-default-enabled",
            "platform": "linux",
            "linuxMonitor": "on",
        },
    )

    assert response.status_code == 302
    node = Node.objects.get(tenant=tenant, node_name="collector-default-enabled")
    assert node.enabled is True


@pytest.mark.django_db
def test_collector_run_script_renders_with_tenant_context(client, billing_plan):
    user = make_user("collector-install-user")
    user.is_staff = True
    user.save(update_fields=["is_staff"])
    tenant = make_tenant("tenant-install", "ti", billing_plan)
    TenantUser.objects.create(user=user, tenant=tenant, role=TenantRole.ADMIN)
    node = make_node(tenant, "collector-install")

    client.force_login(user)
    response = client.get(reverse("run_script_collector", args=[secure_id(tenant), node.node_id]))

    assert response.status_code == 200
    assert response.context["tenant_name"] == tenant.name
    assert response.context["role"] == TenantRole.ADMIN
    assert b"Agent installation command" in response.content


@pytest.mark.django_db
def test_collector_edit_renders_and_updates_collector(client, billing_plan):
    user = make_user("collector-edit-user")
    tenant = make_tenant("tenant-edit", "te", billing_plan)
    TenantUser.objects.create(user=user, tenant=tenant, role=TenantRole.EDITOR)
    node = make_node(tenant, "collector-before")
    sha = "c" * 64
    supervisor_target = SupervisorVersion.objects.create(version="0.4.0")
    SupervisorVersionArtifact.objects.create(version=supervisor_target, arch="amd64", sha256=sha)
    alloy_target = AlloyVersion.objects.create(version="v1.12.0")
    AlloyVersionArtifact.objects.create(
        version=alloy_target,
        arch="amd64",
        package_format="deb",
        sha256=sha,
    )

    client.force_login(user)
    edit_url = reverse("edit_collector", args=[secure_id(tenant), node.node_id])

    response = client.get(edit_url)
    assert response.status_code == 200
    assert response.context["role"] == TenantRole.EDITOR
    assert b"Collector Details" in response.content
    assert b"Linux Collection" in response.content

    response = client.post(
        edit_url,
        {
            "node_name": "collector-after",
            "enabled": "on",
            "updates-target_supervisor_version": str(supervisor_target.pk),
            "updates-target_alloy_version": str(alloy_target.pk),
            "linuxMonitor": "on",
        },
    )
    assert response.status_code == 302
    node.refresh_from_db()
    assert node.node_name == "collector-after"
    assert node.target_supervisor_version == supervisor_target
    assert node.target_alloy_version == alloy_target
    assert node.features.linuxMonitor is True


@pytest.mark.django_db
def test_collector_edit_does_not_sync_grafana(
        client,
        billing_plan,
        monkeypatch,
):
    sync_gitops_resources = Mock()
    monkeypatch.setattr("config.gitops.sync.sync_gitops_resources", sync_gitops_resources)

    user = make_user("collector-edit-no-grafana-user")
    tenant = make_tenant("tenant-edit-grafana", "teg", billing_plan)
    TenantUser.objects.create(user=user, tenant=tenant, role=TenantRole.EDITOR)
    node = make_node(tenant, "collector-before")

    client.force_login(user)
    edit_url = reverse("edit_collector", args=[secure_id(tenant), node.node_id])
    response = client.post(
        edit_url,
        {
            "node_name": "collector-before",
            "enabled": "on",
            "linuxMonitor": "on",
            "containerMonitor": "on",
        },
    )

    assert response.status_code == 302
    sync_gitops_resources.assert_not_called()


@pytest.mark.django_db
def test_collector_edit_syncs_apisix_when_enabled_state_changes(client, billing_plan, monkeypatch):
    sync_apisix = Mock()
    monkeypatch.setattr("ui.views._sync_apisix_consumers_after_commit", sync_apisix)

    user = make_user("collector-disable-user")
    tenant = make_tenant("tenant-disable", "tdis", billing_plan)
    TenantUser.objects.create(user=user, tenant=tenant, role=TenantRole.EDITOR)
    node = make_node(tenant, "collector-before")

    client.force_login(user)
    response = client.post(
        reverse("edit_collector", args=[secure_id(tenant), node.node_id]),
        {
            "node_name": "collector-before",
            "linuxMonitor": "on",
        },
    )

    assert response.status_code == 302
    node.refresh_from_db()
    assert node.enabled is False
    sync_apisix.assert_called_once_with(tenant, user)


@pytest.mark.django_db
def test_collector_delete_syncs_apisix(client, billing_plan, monkeypatch):
    sync_apisix = Mock()
    monkeypatch.setattr("ui.views._sync_apisix_consumers_after_commit", sync_apisix)

    user = make_user("collector-delete-user")
    tenant = make_tenant("tenant-delete", "tdel", billing_plan)
    TenantUser.objects.create(user=user, tenant=tenant, role=TenantRole.ADMIN)
    node = make_node(tenant, "collector-delete")

    client.force_login(user)
    response = client.post(reverse("delete_collector", args=[secure_id(tenant), node.node_id]))

    assert response.status_code == 302
    assert not Node.objects.filter(pk=node.pk).exists()
    sync_apisix.assert_called_once_with(tenant, user)


@pytest.mark.django_db
def test_collector_edit_redis_push_sees_new_file_monitors(
        client,
        billing_plan,
        django_capture_on_commit_callbacks,
        monkeypatch,
):
    payloads = []

    def capture_push(features):
        file_monitors = (
            active_file_monitor_payload(features.node)
            if features.fileMonitor and features.node.enabled
            else []
        )
        payloads.append(
            {
                "fileMonitor": features.fileMonitor and bool(file_monitors),
                "fileMonitors": file_monitors,
            }
        )

    monkeypatch.setattr("config.models.NodeFeatures._push_to_redis", capture_push)

    user = make_user("collector-edit-redis-user")
    tenant = make_tenant("tenant-edit-redis", "ter", billing_plan)
    TenantUser.objects.create(user=user, tenant=tenant, role=TenantRole.EDITOR)
    node = make_node(tenant, "collector-before")

    client.force_login(user)
    with django_capture_on_commit_callbacks(execute=True):
        response = client.post(
            reverse("edit_collector", args=[secure_id(tenant), node.node_id]),
            {
                "node_name": "collector-before",
                "enabled": "on",
                "linuxMonitor": "on",
                "fileMonitor": "on",
                "file_monitors-TOTAL_FORMS": "3",
                "file_monitors-INITIAL_FORMS": "0",
                "file_monitors-MIN_NUM_FORMS": "0",
                "file_monitors-MAX_NUM_FORMS": "3",
                "file_monitors-0-label": "App",
                "file_monitors-0-file_path": "/var/log/app.log",
                "file_monitors-0-enabled": "on",
            },
        )

    assert response.status_code == 302
    assert payloads
    assert all(payload["fileMonitor"] is True for payload in payloads)
    assert all(payload["fileMonitors"] == [{"label": "App", "path": "/var/log/app.log"}] for payload in payloads)


@pytest.mark.django_db
def test_team_admin_adds_member_by_email(client, billing_plan):
    admin = make_user("team-admin")
    member = User.objects.create_user(
        username="team-member",
        email="member@example.com",
        password="password",
    )
    tenant = make_tenant("tenant-team", "tt", billing_plan)
    TenantUser.objects.create(user=admin, tenant=tenant, role=TenantRole.ADMIN)

    client.force_login(admin)
    response = client.post(
        reverse("settings_team_add", args=[secure_id(tenant)]),
        {"email": "MEMBER@example.com", "role": TenantRole.VIEWER},
    )

    assert response.status_code == 302
    assert response.url == reverse("settings_team", args=[secure_id(tenant)])
    membership = TenantUser.objects.get(user=member, tenant=tenant)
    assert membership.role == TenantRole.VIEWER

    response = client.get(reverse("settings_team", args=[secure_id(tenant)]))
    assert response.status_code == 200
    assert b"<th>Email</th>" in response.content
    assert b"member@example.com" in response.content


@pytest.mark.django_db
def test_team_admin_cannot_add_duplicate_member_by_email(client, billing_plan):
    admin = make_user("team-duplicate-admin")
    member = User.objects.create_user(
        username="team-duplicate-member",
        email="duplicate@example.com",
        password="password",
    )
    tenant = make_tenant("tenant-duplicates", "tdp", billing_plan)
    TenantUser.objects.create(user=admin, tenant=tenant, role=TenantRole.ADMIN)
    TenantUser.objects.create(user=member, tenant=tenant, role=TenantRole.VIEWER)

    client.force_login(admin)
    response = client.post(
        reverse("settings_team_add", args=[secure_id(tenant)]),
        {"email": "duplicate@example.com", "role": TenantRole.EDITOR},
    )

    assert response.status_code == 200
    assert b"This account is already in your team." in response.content
    assert TenantUser.objects.filter(user=member, tenant=tenant).count() == 1
    assert TenantUser.objects.get(user=member, tenant=tenant).role == TenantRole.VIEWER


@pytest.mark.django_db
def test_dashboard_live_payload_serializes_tenant_status_counts(billing_plan):
    tenant = make_tenant("tenant-live", "tl", billing_plan)
    make_node(tenant, "online", last_seen_at=timezone.now(), supervisor_version="0.8.0")
    make_node(tenant, "zz-disabled", enabled=False)

    payload = dashboard_live_payload(tenant)

    assert payload["type"] == "dashboard.status"
    assert payload["nodes_online"] == 1
    assert payload["status_counts"][NodeStatus.ONLINE] == 1
    assert payload["status_counts"][NodeStatus.DISABLED] == 1
    assert payload["collectors"][0]["node_id"]
    assert payload["collectors"][0]["supervisor_version"] == "0.8.0"
    assert payload["last_signal_at"]


def test_dashboard_websocket_route_is_registered():
    assert websocket_urlpatterns[0].pattern._route == "ws/<slug:tenant_id_secure>/dashboard/"
    assert websocket_urlpatterns[0].name == "dashboard_ws"
