from datetime import timedelta
from unittest.mock import Mock

import pytest
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.urls import reverse
from django.utils import timezone

from agent.models import CollectorAuth
from config.models import (
    AuditLog,
    Node,
    NodeFeatures,
    NodeToken,
    NodeStatus,
    Tenant,
    TenantBilling,
    TenantBillingStatus,
    TenantLimit,
    TenantRole,
    TenantUsage,
    TenantUser,
)
from config.tasks import clean_token, recheck_tenant_usage


@pytest.fixture(autouse=True)
def external_side_effects(monkeypatch):
    monkeypatch.setattr("config.models.create_tenant", Mock())
    monkeypatch.setattr("config.models.delete_tenant", Mock())
    monkeypatch.setattr("config.models.update_grafana_org_mapping", Mock())
    monkeypatch.setattr("config.models.logout_user_from_grafana_using_email", Mock())
    monkeypatch.setattr("agent.views.NodeFeatures._push_to_redis", Mock())
    monkeypatch.setattr("config.models.NodeFeatures._push_to_redis", Mock())


@pytest.fixture
def billing_plan(db):
    return TenantBilling.objects.create(name="Default", price_per_node=400)


def make_user(username):
    return User.objects.create_user(username=username, email=f"{username}@example.com", password="password")


def make_tenant(name, prefix, billing_plan, *, billing_status=TenantBillingStatus.ACTIVE, max_nodes=5):
    tenant = Tenant.objects.create(
        name=name,
        random_prefix=prefix,
        grafana_org_id=100 + len(name),
        billing_status=billing_status,
    )
    TenantLimit.objects.create(tenant=tenant, billing=billing_plan, max_nodes=max_nodes)
    TenantUsage.objects.create(tenant=tenant)
    return tenant


def make_member(user, tenant, role=TenantRole.ADMIN):
    return TenantUser.objects.create(user=user, tenant=tenant, role=role)


def make_node(tenant, name="node"):
    node = Node.objects.create(tenant=tenant, node_name=name)
    NodeFeatures.objects.create(node=node)
    return node


def secure_id(tenant):
    return f"{tenant.random_prefix}-{tenant.name}"


@pytest.mark.django_db
def test_collector_views_scope_node_by_tenant(client, billing_plan):
    user = make_user("alice")
    user.is_staff = True
    user.save(update_fields=["is_staff"])
    tenant_a = make_tenant("tenant-a", "ta", billing_plan)
    tenant_b = make_tenant("tenant-b", "tb", billing_plan)
    make_member(user, tenant_a, TenantRole.ADMIN)
    make_member(user, tenant_b, TenantRole.ADMIN)
    node_b = make_node(tenant_b, "belongs-to-b")

    client.force_login(user)

    edit_url = reverse("edit_collector", args=[secure_id(tenant_a), node_b.node_id])
    run_url = reverse("run_script_collector", args=[secure_id(tenant_a), node_b.node_id])
    delete_url = reverse("delete_collector", args=[secure_id(tenant_a), node_b.node_id])

    assert client.get(edit_url).status_code == 404
    assert client.get(run_url).status_code == 404
    assert client.post(delete_url).status_code == 404
    assert Node.objects.filter(pk=node_b.pk).exists()


@pytest.mark.django_db
def test_installer_token_is_one_time(client, billing_plan):
    user = make_user("bob")
    tenant = make_tenant("tenant-c", "tc", billing_plan)
    make_member(user, tenant, TenantRole.ADMIN)
    node = make_node(tenant)
    token = node.generate_token(actor=user)

    url = reverse("installer", args=[token.token])
    first_response = client.get(url)
    assert first_response.status_code == 200

    token.refresh_from_db()
    assert token.used_at is not None
    auth = CollectorAuth.objects.get(node=node)
    assert auth.config_consumer_name == f"{node.node_id.hex}-config"
    assert auth.mimir_consumer_name == f"{node.node_id.hex}-mimir"
    assert auth.loki_consumer_name == f"{node.node_id.hex}-loki"
    assert auth.config_password
    assert AuditLog.objects.filter(action="node_token.used", target_id=str(token.pk)).exists()

    second_response = client.get(url)
    assert second_response.status_code == 410


@pytest.mark.django_db
def test_installer_repairs_blank_collector_auth_fields(client, billing_plan):
    tenant = make_tenant("tenant-auth-repair", "tar", billing_plan)
    node = make_node(tenant)
    auth = CollectorAuth.objects.create(node=node)
    token = node.generate_token()

    response = client.get(reverse("installer", args=[token.token]))

    assert response.status_code == 200
    auth.refresh_from_db()
    assert auth.config_consumer_name == f"{node.node_id.hex}-config"
    assert auth.mimir_consumer_name == f"{node.node_id.hex}-mimir"
    assert auth.loki_consumer_name == f"{node.node_id.hex}-loki"
    assert auth.config_password
    assert AuditLog.objects.filter(
        action="node_token.used",
        target_id=str(token.pk),
        metadata__repaired_credentials=True,
    ).exists()


@pytest.mark.django_db
def test_expired_revoked_and_suspended_tokens_are_rejected(client, billing_plan, settings):
    settings.BILLING_ENABLED = True
    tenant = make_tenant("tenant-d", "td", billing_plan)
    node = make_node(tenant)

    expired = node.generate_token()
    expired.expires_at = timezone.now() - timedelta(minutes=1)
    expired.save(update_fields=["expires_at"])
    assert client.get(reverse("installer", args=[expired.token])).status_code == 410

    revoked = node.generate_token()
    revoked.revoked_at = timezone.now()
    revoked.save(update_fields=["revoked_at"])
    assert client.get(reverse("installer", args=[revoked.token])).status_code == 410

    suspended = node.generate_token()
    tenant.billing_status = TenantBillingStatus.SUSPENDED
    tenant.save(update_fields=["billing_status"])
    assert client.get(reverse("installer", args=[suspended.token])).status_code == 410
    with pytest.raises(ValidationError):
        node.generate_token()
    assert not CollectorAuth.objects.filter(node=node).exists()


@pytest.mark.django_db
def test_generating_token_revokes_existing_active_token(billing_plan):
    tenant = make_tenant("tenant-e", "te", billing_plan)
    node = make_node(tenant)

    first = node.generate_token()
    second = node.generate_token()

    first.refresh_from_db()
    assert first.revoked_at is not None
    assert second.revoked_at is None
    assert NodeToken.objects.filter(node=node, used_at__isnull=True, revoked_at__isnull=True).count() == 1


@pytest.mark.django_db
def test_node_token_default_expiry_is_10_minutes(settings, billing_plan):
    settings.NODE_TOKEN_TTL_MINUTES = 10
    tenant = make_tenant("tenant-token-ttl", "tt", billing_plan)
    node = make_node(tenant)

    before = timezone.now()
    token = node.generate_token()
    after = timezone.now()

    assert before + timedelta(minutes=10) <= token.expires_at <= after + timedelta(minutes=10)


@pytest.mark.django_db
def test_clean_token_deletes_only_expired_tokens(billing_plan):
    tenant = make_tenant("tenant-clean-token", "ct", billing_plan)
    node = make_node(tenant)
    expired = node.generate_token()
    expired.expires_at = timezone.now() - timedelta(seconds=1)
    expired.save(update_fields=["expires_at"])
    active = node.generate_token()

    clean_token()

    assert not NodeToken.objects.filter(pk=expired.pk).exists()
    assert NodeToken.objects.filter(pk=active.pk).exists()


@pytest.mark.django_db
def test_recheck_tenant_usage_repairs_node_counts_and_missing_usage(billing_plan):
    stale_tenant = make_tenant("tenant-stale-usage", "tsu", billing_plan)
    stale_usage = TenantUsage.objects.get(tenant=stale_tenant)
    stale_usage.nodes = 99
    stale_usage.clusters = 2
    stale_usage.vcpus = 8
    stale_usage.save(update_fields=["nodes", "clusters", "vcpus", "updated_at"])
    make_node(stale_tenant, "node-a")
    make_node(stale_tenant, "node-b")

    missing_tenant = make_tenant("tenant-missing-usage", "tmu", billing_plan)
    TenantUsage.objects.filter(tenant=missing_tenant).delete()
    make_node(missing_tenant, "node-c")

    result = recheck_tenant_usage()

    stale_usage.refresh_from_db()
    missing_usage = TenantUsage.objects.get(tenant=missing_tenant)
    assert result == {"tenants": 2, "created": 1, "updated": 1}
    assert stale_usage.nodes == 2
    assert stale_usage.clusters == 2
    assert stale_usage.vcpus == 8
    assert missing_usage.nodes == 1
    assert missing_usage.clusters == 0
    assert missing_usage.vcpus == 0


@pytest.mark.django_db
def test_node_status_thresholds(billing_plan):
    tenant = make_tenant("tenant-f", "tf", billing_plan)
    node = make_node(tenant)

    assert node.status == NodeStatus.NEVER_CONNECTED

    node.last_config_poll_at = timezone.now() - timedelta(minutes=3)
    assert node.status == NodeStatus.ONLINE

    node.last_config_poll_at = timezone.now() - timedelta(minutes=10)
    assert node.status == NodeStatus.WARNING

    node.last_config_poll_at = timezone.now() - timedelta(minutes=31)
    assert node.status == NodeStatus.OFFLINE

    node.enabled = False
    assert node.status == NodeStatus.DISABLED


@pytest.mark.django_db
def test_oidc_debug_page_is_staff_only(client, billing_plan):
    tenant = make_tenant("tenant-g", "tg", billing_plan)
    staff = make_user("staff")
    staff.is_staff = True
    staff.save(update_fields=["is_staff"])
    make_member(staff, tenant, TenantRole.ADMIN)

    non_staff = make_user("viewer")
    make_member(non_staff, tenant, TenantRole.VIEWER)
    url = reverse("oidc_claims_debug", args=[secure_id(tenant)])

    client.force_login(non_staff)
    assert client.get(url).status_code == 302

    client.force_login(staff)
    response = client.get(url)
    assert response.status_code == 200
    assert b"nova-tg-admin" in response.content
