from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from agent.models import CollectorAuth
from config.models import (
    GitOpsResource,
    Node,
    NodeFeatures,
    NodeStatus,
    NodeToken,
    TenantBillingStatus,
    TenantRole,
    TenantUsage,
    TenantUser,
)

pytestmark = pytest.mark.django_db


def test_tenant_list_requires_login_and_lists_user_tenants(
        client,
        user_factory,
        tenant_factory,
        member_factory,
        login_user,
):
    user = user_factory("tenant-list-user")
    tenant = tenant_factory("tenant-list", prefix="tl")
    other_tenant = tenant_factory("tenant-list-other", prefix="tlo")
    member_factory(user, tenant, TenantRole.VIEWER)

    response = client.get(reverse("tenant_list"))
    assert response.status_code == 302
    assert "/accounts/login/" in response["Location"]

    login_user(client, user)
    response = client.get(reverse("tenant_list"))

    assert response.status_code == 200
    assert list(response.context["tenants"]) == [tenant]
    assert other_tenant.name.encode() not in response.content


@pytest.mark.parametrize(
    ("billing_status", "notice"),
    [
        (TenantBillingStatus.ACTIVE, None),
        (TenantBillingStatus.GRACE, b"Billing is in grace period"),
        (TenantBillingStatus.RESTRICTED, b"Billing is restricted"),
        (TenantBillingStatus.SUSPENDED, b"Billing is suspended"),
    ],
)
def test_dashboard_renders_status_billing_and_grafana_state(
        client,
        settings,
        user_factory,
        tenant_factory,
        member_factory,
        node_factory,
        login_user,
        secure_tenant_id,
        billing_status,
        notice,
):
    settings.GRAFANA_URL = "https://grafana.example"
    user = user_factory("dashboard-user")
    tenant = tenant_factory(
        f"dashboard-{billing_status}",
        prefix=f"d{billing_status[0]}",
        billing_status=billing_status,
        usage_nodes=5,
        usage_clusters=1,
        usage_vcpus=4,
        max_nodes=10,
    )
    member_factory(user, tenant, TenantRole.ADMIN)
    now = timezone.now()
    node_factory(tenant, "online", last_config_poll_at=now - timedelta(minutes=1))
    node_factory(tenant, "warning", last_config_poll_at=now - timedelta(minutes=10))
    node_factory(tenant, "offline", last_config_poll_at=now - timedelta(minutes=31))
    node_factory(tenant, "new")
    node_factory(tenant, "disabled", enabled=False)

    login_user(client, user)
    response = client.get(reverse("dashboard", args=[secure_tenant_id(tenant)]))

    assert response.status_code == 200
    assert response.context["grafana_url"] == f"https://grafana.example/?orgId={tenant.grafana_org_id}"
    assert response.context["status_counts"][NodeStatus.ONLINE] == 1
    assert response.context["status_counts"][NodeStatus.WARNING] == 1
    assert response.context["status_counts"][NodeStatus.OFFLINE] == 1
    assert response.context["status_counts"][NodeStatus.NEVER_CONNECTED] == 1
    assert response.context["status_counts"][NodeStatus.DISABLED] == 1
    assert response.context["resource_usage"][0]["percent"] == 50
    assert response.context["billing_status"] == billing_status
    if notice:
        assert notice in response.content


def test_dashboard_without_grafana_org_uses_base_grafana_url(
        client,
        settings,
        user_factory,
        tenant_factory,
        member_factory,
        login_user,
        secure_tenant_id,
):
    settings.GRAFANA_EXTERNAL_URL = "https://grafana.example/"
    user = user_factory("dashboard-base-grafana")
    tenant = tenant_factory("dashboard-base", prefix="db", grafana_org_id=None)
    tenant.grafana_org_id = None
    tenant.save(update_fields=["grafana_org_id"])
    member_factory(user, tenant, TenantRole.VIEWER)

    login_user(client, user)
    response = client.get(reverse("dashboard", args=[secure_tenant_id(tenant)]))

    assert response.status_code == 200
    assert response.context["grafana_url"] == "https://grafana.example"


def test_collector_list_is_tenant_scoped(
        client,
        user_factory,
        tenant_factory,
        member_factory,
        node_factory,
        login_user,
        secure_tenant_id,
):
    user = user_factory("collector-list-user")
    tenant = tenant_factory("collector-list", prefix="cl")
    other = tenant_factory("collector-list-other", prefix="clo")
    member_factory(user, tenant, TenantRole.EDITOR)
    visible = node_factory(tenant, "visible-collector", supervisor_version="0.7.0")
    hidden = node_factory(other, "hidden-collector")

    login_user(client, user)
    response = client.get(reverse("list_collectors", args=[secure_tenant_id(tenant)]))

    assert response.status_code == 200
    assert visible.node_name.encode() in response.content
    assert b"0.7.0" in response.content
    assert hidden.node_name.encode() not in response.content


@pytest.mark.parametrize(
    "route_name,args_builder",
    [
        ("add_collector", lambda tenant, node, member: [tenant]),
        ("run_script_collector", lambda tenant, node, member: [tenant, node.node_id]),
        ("edit_collector", lambda tenant, node, member: [tenant, node.node_id]),
        ("delete_collector", lambda tenant, node, member: [tenant, node.node_id]),
        ("settings_team", lambda tenant, node, member: [tenant]),
        ("settings_team_add", lambda tenant, node, member: [tenant]),
        ("settings_team_edit", lambda tenant, node, member: [tenant, member.pk]),
        ("settings_team_delete", lambda tenant, node, member: [tenant, member.pk]),
        ("settings_team_limits", lambda tenant, node, member: [tenant]),
    ],
)
def test_viewer_is_forbidden_from_mutating_or_admin_routes(
        client,
        user_factory,
        tenant_factory,
        member_factory,
        node_factory,
        login_user,
        secure_tenant_id,
        route_name,
        args_builder,
):
    user = user_factory("viewer-forbidden")
    tenant = tenant_factory("viewer-forbidden", prefix="vf")
    member = member_factory(user, tenant, TenantRole.VIEWER)
    node = node_factory(tenant, "viewer-node")

    login_user(client, user)
    args = args_builder(secure_tenant_id(tenant), node, member)
    response = client.get(reverse(route_name, args=args))

    assert response.status_code == 403


def test_editor_can_edit_but_cannot_delete_collectors_or_admin_team(
        client,
        user_factory,
        tenant_factory,
        member_factory,
        node_factory,
        login_user,
        secure_tenant_id,
):
    user = user_factory("editor-role-user")
    tenant = tenant_factory("editor-role", prefix="er")
    member_factory(user, tenant, TenantRole.EDITOR)
    node = node_factory(tenant, "editor-node")

    login_user(client, user)

    assert client.get(reverse("edit_collector", args=[secure_tenant_id(tenant), node.node_id])).status_code == 200
    assert client.get(reverse("delete_collector", args=[secure_tenant_id(tenant), node.node_id])).status_code == 403
    assert client.get(reverse("settings_team", args=[secure_tenant_id(tenant)])).status_code == 403


def test_collector_add_get_valid_post_invalid_post_and_limit_reached(
        client,
        user_factory,
        tenant_factory,
        member_factory,
        login_user,
        secure_tenant_id,
        django_capture_on_commit_callbacks,
        monkeypatch,
):
    monkeypatch.setattr(
        "config.gitops.plugins.apisix.seal_secret_data",
        lambda *, name, namespace, data: {key: f"sealed-{name}-{key}" for key in data},
    )
    user = user_factory("collector-add-user", is_staff=True)
    tenant = tenant_factory("collector-add", prefix="ca", max_nodes=1)
    member_factory(user, tenant, TenantRole.ADMIN)

    login_user(client, user)
    add_url = reverse("add_collector", args=[secure_tenant_id(tenant)])

    response = client.get(add_url)
    assert response.status_code == 200
    assert b"Choose collector type" in response.content
    assert b"platform=linux" in response.content

    invalid = client.post(add_url, {"platform": "linux", "node_name": "invalid", "enabled": "on"})
    assert invalid.status_code == 200
    assert b"Enable at least one Linux monitoring or logging feature" in invalid.content
    assert Node.objects.filter(tenant=tenant).count() == 0

    with django_capture_on_commit_callbacks(execute=True):
        valid = client.post(
            add_url,
            {
                "platform": "linux",
                "node_name": "collector-created",
                "enabled": "on",
                "linuxMonitor": "on",
            },
        )
    assert valid.status_code == 302
    node = Node.objects.get(tenant=tenant, node_name="collector-created")
    auth = CollectorAuth.objects.get(node=node)
    assert auth.config_consumer_name == f"{node.node_id.hex}-config"
    assert auth.mimir_consumer_name == f"{node.node_id.hex}-mimir"
    assert auth.loki_consumer_name == f"{node.node_id.hex}-loki"
    assert auth.provisioner_backend == "apisix-crd"
    assert auth.provisioned_at is not None
    assert NodeFeatures.objects.get(node=node).linuxMonitor is True
    assert NodeToken.objects.filter(node=node, used_at__isnull=True, revoked_at__isnull=True).count() == 1
    resources = list(GitOpsResource.objects.filter(plugin="apisix", tenant=tenant))
    assert len(resources) == 6
    assert {
               resource.desired_manifest["metadata"]["labels"]["nova.proxima.io/service"]
               for resource in resources
           } == {"config", "mimir", "loki"}
    assert TenantUsage.objects.get(tenant=tenant).nodes == 1

    limited = client.get(add_url)
    assert limited.status_code == 302
    assert limited["Location"] == reverse("list_collectors", args=[secure_tenant_id(tenant)])


def test_collector_add_and_edit_sync_file_monitors(
        client,
        user_factory,
        tenant_factory,
        member_factory,
        login_user,
        secure_tenant_id,
):
    user = user_factory("collector-files-user", is_staff=True)
    tenant = tenant_factory("collector-files", prefix="cf", max_nodes=2)
    member_factory(user, tenant, TenantRole.ADMIN)
    tenant_id = secure_tenant_id(tenant)

    login_user(client, user)
    add_response = client.post(
        reverse("add_collector", args=[tenant_id]),
        {
            "platform": "linux",
            "node_name": "collector-with-files",
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
            "file_monitors-1-label": "Audit",
            "file_monitors-1-file_path": "/var/log/audit.log",
        },
    )

    assert add_response.status_code == 302
    node = Node.objects.get(tenant=tenant, node_name="collector-with-files")
    monitors = list(node.file_monitors.order_by("file_path"))
    assert [monitor.file_path for monitor in monitors] == ["/var/log/app.log", "/var/log/audit.log"]
    assert monitors[0].enabled is True
    assert monitors[1].enabled is False
    assert node.features.fileMonitor is True

    edit_response = client.post(
        reverse("edit_collector", args=[tenant_id, node.node_id]),
        {
            "node_name": "collector-with-files",
            "enabled": "on",
            "linuxMonitor": "on",
            "fileMonitor": "on",
            "file_monitors-TOTAL_FORMS": "3",
            "file_monitors-INITIAL_FORMS": "2",
            "file_monitors-MIN_NUM_FORMS": "0",
            "file_monitors-MAX_NUM_FORMS": "3",
            "file_monitors-0-id": str(monitors[0].pk),
            "file_monitors-0-label": "Application",
            "file_monitors-0-file_path": "/var/log/application.log",
            "file_monitors-0-enabled": "on",
            "file_monitors-1-id": str(monitors[1].pk),
            "file_monitors-1-label": "Audit",
            "file_monitors-1-file_path": "/var/log/audit.log",
            "file_monitors-1-DELETE": "on",
            "file_monitors-2-label": "Nginx",
            "file_monitors-2-file_path": "/var/log/nginx/access.log",
            "file_monitors-2-enabled": "on",
        },
    )

    assert edit_response.status_code == 302
    assert list(node.file_monitors.order_by("file_path").values_list("file_path", flat=True)) == [
        "/var/log/application.log",
        "/var/log/nginx/access.log",
    ]

    invalid_response = client.post(
        reverse("edit_collector", args=[tenant_id, node.node_id]),
        {
            "node_name": "collector-with-files",
            "enabled": "on",
            "linuxMonitor": "on",
            "fileMonitor": "on",
            "file_monitors-TOTAL_FORMS": "3",
            "file_monitors-INITIAL_FORMS": "2",
            "file_monitors-MIN_NUM_FORMS": "0",
            "file_monitors-MAX_NUM_FORMS": "3",
            "file_monitors-0-label": "One",
            "file_monitors-0-file_path": "/var/log/app.log",
            "file_monitors-0-enabled": "on",
            "file_monitors-1-label": "Two",
            "file_monitors-1-file_path": "/var/log/../log/app.log",
            "file_monitors-1-enabled": "on",
        },
    )

    assert invalid_response.status_code == 200
    assert b"duplicate path" in invalid_response.content


def test_collector_run_script_uses_existing_active_token(
        client,
        user_factory,
        tenant_factory,
        member_factory,
        node_factory,
        login_user,
        secure_tenant_id,
):
    user = user_factory("run-script-user", is_staff=True)
    tenant = tenant_factory("run-script", prefix="rs")
    member_factory(user, tenant, TenantRole.ADMIN)
    node = node_factory(tenant, "run-script-node")
    token = node.generate_token(actor=user)

    login_user(client, user)
    response = client.get(reverse("run_script_collector", args=[secure_tenant_id(tenant), node.node_id]))

    assert response.status_code == 200
    assert response.context["token"] == token
    assert token.token.encode() in response.content


def test_collector_edit_updates_and_rejects_invalid_feature_selection(
        client,
        user_factory,
        tenant_factory,
        member_factory,
        node_factory,
        login_user,
        secure_tenant_id,
):
    user = user_factory("collector-edit-user")
    tenant = tenant_factory("collector-edit", prefix="ce")
    member_factory(user, tenant, TenantRole.ADMIN)
    node = node_factory(tenant, "collector-before")
    edit_url = reverse("edit_collector", args=[secure_tenant_id(tenant), node.node_id])

    login_user(client, user)
    assert client.get(edit_url).status_code == 200

    invalid = client.post(edit_url, {"node_name": "collector-invalid", "enabled": "on"})
    assert invalid.status_code == 200
    assert b"Enable at least one Linux monitoring or logging feature" in invalid.content

    valid = client.post(
        edit_url,
        {
            "node_name": "collector-after",
            "enabled": "on",
            "linuxMonitor": "on",
            "dockerLogs": "on",
        },
    )
    assert valid.status_code == 302
    node.refresh_from_db()
    node.features.refresh_from_db()
    assert node.node_name == "collector-after"
    assert node.features.dockerLogs is True


def test_collector_object_routes_do_not_cross_tenant_boundaries(
        client,
        user_factory,
        tenant_factory,
        member_factory,
        node_factory,
        login_user,
        secure_tenant_id,
):
    user = user_factory("cross-tenant-user", is_staff=True)
    tenant_a = tenant_factory("cross-a", prefix="xa")
    tenant_b = tenant_factory("cross-b", prefix="xb")
    member_factory(user, tenant_a, TenantRole.ADMIN)
    member_factory(user, tenant_b, TenantRole.ADMIN)
    node_b = node_factory(tenant_b, "belongs-to-b")

    login_user(client, user)

    assert client.get(reverse("edit_collector", args=[secure_tenant_id(tenant_a), node_b.node_id])).status_code == 404
    assert client.get(
        reverse("run_script_collector", args=[secure_tenant_id(tenant_a), node_b.node_id])).status_code == 404
    assert client.post(
        reverse("delete_collector", args=[secure_tenant_id(tenant_a), node_b.node_id])).status_code == 404
    assert Node.objects.filter(pk=node_b.pk).exists()


def test_collector_delete_confirmation_and_post_deletes_node_and_decrements_usage(
        client,
        user_factory,
        tenant_factory,
        member_factory,
        node_factory,
        login_user,
        secure_tenant_id,
):
    user = user_factory("collector-delete-user")
    tenant = tenant_factory("collector-delete", prefix="cd", usage_nodes=1)
    member_factory(user, tenant, TenantRole.ADMIN)
    node = node_factory(tenant, "delete-me")
    url = reverse("delete_collector", args=[secure_tenant_id(tenant), node.node_id])

    login_user(client, user)
    assert client.get(url).status_code == 200

    response = client.post(url)
    assert response.status_code == 302
    assert not Node.objects.filter(pk=node.pk).exists()
    assert TenantUsage.objects.get(tenant=tenant).nodes == 0


def test_oidc_claims_debug_requires_staff_and_renders_claims(
        client,
        user_factory,
        tenant_factory,
        member_factory,
        login_user,
        secure_tenant_id,
):
    tenant = tenant_factory("debug-claims", prefix="dc")
    staff = user_factory("debug-staff", is_staff=True)
    non_staff = user_factory("debug-non-staff")
    member_factory(staff, tenant, TenantRole.ADMIN)
    member_factory(non_staff, tenant, TenantRole.VIEWER)
    url = reverse("oidc_claims_debug", args=[secure_tenant_id(tenant)])

    login_user(client, non_staff)
    assert client.get(url).status_code == 302

    login_user(client, staff)
    response = client.get(url)
    assert response.status_code == 200
    assert b"nova-dc-admin" in response.content


def test_team_settings_add_edit_delete_member(
        client,
        user_factory,
        tenant_factory,
        member_factory,
        login_user,
        secure_tenant_id,
):
    admin = user_factory("team-admin")
    new_user = user_factory("team-member")
    replacement_user = user_factory("team-replacement")
    tenant = tenant_factory("team-settings", prefix="ts")
    member_factory(admin, tenant, TenantRole.ADMIN)

    login_user(client, admin)
    list_url = reverse("settings_team", args=[secure_tenant_id(tenant)])
    add_url = reverse("settings_team_add", args=[secure_tenant_id(tenant)])

    assert client.get(list_url).status_code == 200
    assert client.get(add_url).status_code == 200

    added = client.post(add_url, {"username": new_user.username, "role": TenantRole.VIEWER})
    assert added.status_code == 302
    membership = TenantUser.objects.get(user=new_user, tenant=tenant)
    assert membership.role == TenantRole.VIEWER

    duplicate = client.post(add_url, {"username": new_user.username, "role": TenantRole.EDITOR})
    assert duplicate.status_code == 200
    assert b"already in your team" in duplicate.content

    missing = client.post(add_url, {"username": "does-not-exist", "role": TenantRole.EDITOR})
    assert missing.status_code == 200
    assert b"Unable to add this account" in missing.content

    edit_url = reverse("settings_team_edit", args=[secure_tenant_id(tenant), membership.pk])
    edited = client.post(edit_url, {"username": replacement_user.username, "role": TenantRole.EDITOR})
    assert edited.status_code == 302
    membership.refresh_from_db()
    assert membership.user == new_user
    assert membership.role == TenantRole.EDITOR

    delete_url = reverse("settings_team_delete", args=[secure_tenant_id(tenant), membership.pk])
    assert client.get(delete_url).status_code == 200
    deleted = client.post(delete_url)
    assert deleted.status_code == 302
    assert not TenantUser.objects.filter(pk=membership.pk).exists()


def test_team_member_routes_are_tenant_scoped(
        client,
        user_factory,
        tenant_factory,
        member_factory,
        login_user,
        secure_tenant_id,
):
    admin = user_factory("team-scope-admin")
    member = user_factory("team-scope-member")
    tenant_a = tenant_factory("team-scope-a", prefix="tsa")
    tenant_b = tenant_factory("team-scope-b", prefix="tsb")
    member_factory(admin, tenant_a, TenantRole.ADMIN)
    member_factory(admin, tenant_b, TenantRole.ADMIN)
    member_b = member_factory(member, tenant_b, TenantRole.VIEWER)

    login_user(client, admin)

    assert client.get(reverse("settings_team_edit", args=[secure_tenant_id(tenant_a), member_b.pk])).status_code == 404
    assert client.post(
        reverse("settings_team_delete", args=[secure_tenant_id(tenant_a), member_b.pk])).status_code == 404
    assert TenantUser.objects.filter(pk=member_b.pk).exists()


def test_team_limits_get_update_and_validation_paths(
        client,
        user_factory,
        tenant_factory,
        member_factory,
        login_user,
        secure_tenant_id,
):
    user = user_factory("limits-admin", is_staff=True)
    tenant = tenant_factory(
        "limits",
        prefix="li",
        usage_nodes=2,
        usage_clusters=1,
        usage_vcpus=4,
        max_nodes=5,
        max_clusters=2,
        max_vcpus=8,
    )
    member_factory(user, tenant, TenantRole.ADMIN)
    url = reverse("settings_team_limits", args=[secure_tenant_id(tenant)])

    login_user(client, user)
    assert client.get(url).status_code == 200

    invalid_number = client.post(url, {"max_nodes": "-1", "max_clusters": "2", "max_vcpus": "8"})
    assert invalid_number.status_code == 200
    assert b"Max nodes must be a non-negative integer" in invalid_number.content

    below_usage = client.post(url, {"max_nodes": "1", "max_clusters": "2", "max_vcpus": "8"})
    assert below_usage.status_code == 200
    assert b"Max nodes cannot be lower than current usage" in below_usage.content

    updated = client.post(url, {"max_nodes": "6", "max_clusters": "3", "max_vcpus": "9"})
    assert updated.status_code == 302
    limit = tenant.tenantlimit_set.order_by("-updated_at").first()
    assert limit.max_nodes == 6
    assert limit.max_clusters == 3
    assert limit.max_vcpus == 9


def test_team_limits_without_configured_plan_reports_error(
        client,
        user_factory,
        tenant_factory,
        member_factory,
        login_user,
        secure_tenant_id,
):
    user = user_factory("limits-no-plan-admin", is_staff=True)
    tenant = tenant_factory("limits-no-plan", prefix="lnp", with_limit=False)
    member_factory(user, tenant, TenantRole.ADMIN)

    login_user(client, user)
    response = client.post(
        reverse("settings_team_limits", args=[secure_tenant_id(tenant)]),
        {"max_nodes": "1", "max_clusters": "1", "max_vcpus": "1"},
    )

    assert response.status_code == 200
    assert b"Billing plan is not configured for this team" in response.content
