import pytest
from django.urls import reverse

from config.models import Node, TenantBillingStatus, TenantRole

pytestmark = pytest.mark.django_db


def test_settings_billing_endpoint_should_render_for_authenticated_tenant_member(
        client,
        user_factory,
        tenant_factory,
        member_factory,
        login_user,
        secure_tenant_id,
):
    """Known failure: the URL passes tenant_id_secure but the view accepts only request."""
    user = user_factory("known-billing-user")
    tenant = tenant_factory("known-billing", prefix="kb")
    member_factory(user, tenant, TenantRole.ADMIN)
    login_user(client, user)
    client.raise_request_exception = False

    response = client.get(reverse("settings_billing", args=[secure_tenant_id(tenant)]))

    assert response.status_code == 200


def test_malformed_tenant_slug_should_not_return_server_error(
        client,
        user_factory,
        login_user,
):
    """Known failure: tenant_role_required splits slugs without handling missing '-'."""
    user = user_factory("known-malformed-slug")
    login_user(client, user)
    client.raise_request_exception = False

    response = client.get(reverse("add_collector", args=["badslug"]))

    assert response.status_code in {403, 404}


def test_suspended_tenant_collector_add_should_fail_gracefully_without_500(
        client,
        user_factory,
        tenant_factory,
        member_factory,
        login_user,
        secure_tenant_id,
):
    """Known failure: collector_add creates a node, then Node.generate_token raises ValidationError."""
    user = user_factory("known-suspended-add")
    tenant = tenant_factory(
        "known-suspended-add",
        prefix="ksa",
        billing_status=TenantBillingStatus.SUSPENDED,
        max_nodes=5,
    )
    member_factory(user, tenant, TenantRole.ADMIN)
    login_user(client, user)
    client.raise_request_exception = False

    response = client.post(
        reverse("add_collector", args=[secure_tenant_id(tenant)]),
        {"platform": "linux", "node_name": "blocked", "enabled": "on", "linuxMonitor": "on"},
    )

    assert response.status_code == 302
    assert response["Location"] == reverse("list_collectors", args=[secure_tenant_id(tenant)])
    assert not Node.objects.filter(tenant=tenant, node_name="blocked").exists()


def test_team_settings_should_scope_current_role_to_active_tenant_for_multi_tenant_users(
        client,
        user_factory,
        tenant_factory,
        member_factory,
        login_user,
        secure_tenant_id,
):
    """Known failure: settings_team calls TenantUser.objects.get(user=request.user)."""
    user = user_factory("known-team-scope")
    tenant_a = tenant_factory("known-team-a", prefix="kta")
    tenant_b = tenant_factory("known-team-b", prefix="ktb")
    member_factory(user, tenant_a, TenantRole.ADMIN)
    member_factory(user, tenant_b, TenantRole.ADMIN)
    login_user(client, user)
    client.raise_request_exception = False

    response = client.get(reverse("settings_team", args=[secure_tenant_id(tenant_a)]))

    assert response.status_code == 200
    assert response.context["tenant_name"] == tenant_a.name


def test_dashboard_for_non_member_tenant_should_be_forbidden_or_not_found(
        client,
        user_factory,
        tenant_factory,
        login_user,
        secure_tenant_id,
):
    """Known failure: dashboard renders an empty dashboard for non-members."""
    user = user_factory("known-dashboard-scope")
    tenant = tenant_factory("known-dashboard-other", prefix="kdo")
    login_user(client, user)

    response = client.get(reverse("dashboard", args=[secure_tenant_id(tenant)]))

    assert response.status_code in {403, 404}
