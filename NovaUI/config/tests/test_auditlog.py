from unittest.mock import Mock

import pytest
from auditlog import get_logentry_model
from auditlog.context import set_actor
from django.contrib.auth.models import User
from django.contrib.contenttypes.models import ContentType
from django.urls import reverse

from config.audit import tenant_audit_entries
from config.models import (
    Node,
    NodeFeatures,
    NodeToken,
    Tenant,
    TenantBilling,
    TenantBillingStatus,
    TenantLimit,
    TenantRole,
    TenantUsage,
    TenantUser,
)


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


def make_user(username, *, is_staff=False):
    user = User.objects.create_user(username=username, email=f"{username}@example.com", password="password")
    if is_staff:
        user.is_staff = True
        user.save(update_fields=["is_staff"])
    return user


def make_tenant(name, prefix, billing_plan):
    tenant = Tenant.objects.create(
        name=name,
        random_prefix=prefix,
        grafana_org_id=42,
        billing_status=TenantBillingStatus.ACTIVE,
    )
    TenantLimit.objects.create(tenant=tenant, billing=billing_plan, max_nodes=10)
    TenantUsage.objects.create(tenant=tenant)
    return tenant


def make_node(tenant, name):
    node = Node.objects.create(tenant=tenant, node_name=name)
    NodeFeatures.objects.create(node=node, linuxMonitor=True)
    return node


def secure_id(tenant):
    return f"{tenant.random_prefix}-{tenant.name}"


@pytest.mark.django_db
def test_auditlog_records_tenant_actor_and_masks_node_tokens(billing_plan):
    LogEntry = get_logentry_model()
    user = make_user("audit-token-user")
    tenant = make_tenant("audit-token", "at", billing_plan)
    node = make_node(tenant, "audit-node")

    with set_actor(user):
        token = node.generate_token(actor=user)

    entry = LogEntry.objects.get(
        content_type=ContentType.objects.get_for_model(NodeToken),
        object_pk=str(token.pk),
        action=LogEntry.Action.CREATE,
    )
    assert entry.tenant == tenant
    assert entry.tenant_pk == tenant.pk
    assert entry.tenant_name == tenant.name
    assert entry.tenant_secure_id == secure_id(tenant)
    assert entry.actor == user
    assert token.token not in str(entry.changes)
    assert entry.changes["token"][1] != token.token
    assert entry.changes["token"][1].startswith("*")


@pytest.mark.django_db
def test_tenant_audit_entries_are_scoped_to_one_tenant(billing_plan):
    LogEntry = get_logentry_model()
    user = make_user("audit-scope-user")
    tenant_a = make_tenant("audit-a", "aa", billing_plan)
    tenant_b = make_tenant("audit-b", "ab", billing_plan)

    with set_actor(user):
        make_node(tenant_a, "visible-node")
        make_node(tenant_b, "hidden-node")

    entries = list(tenant_audit_entries(tenant_a))
    assert entries
    assert all(entry.tenant_pk == tenant_a.pk for entry in entries if entry.tenant_pk)
    assert not any(entry.tenant_pk == tenant_b.pk for entry in entries)
    assert LogEntry.objects.filter(tenant=tenant_b).exists()


@pytest.mark.django_db
def test_tenant_audit_page_shows_request_actor_changes(client, billing_plan):
    LogEntry = get_logentry_model()
    user = make_user("audit-page-user", is_staff=True)
    tenant = make_tenant("audit-page", "ap", billing_plan)
    TenantUser.objects.create(user=user, tenant=tenant, role=TenantRole.ADMIN)
    node = make_node(tenant, "collector-before")

    client.force_login(user)
    response = client.post(
        reverse("edit_collector", args=[secure_id(tenant), node.node_id]),
        {
            "node_name": "collector-after",
            "enabled": "on",
            "linuxMonitor": "on",
        },
    )
    assert response.status_code == 302

    node_entries = list(
        LogEntry.objects.filter(
            tenant=tenant,
            actor=user,
            content_type=ContentType.objects.get_for_model(Node),
            object_pk=str(node.pk),
        )
    )
    assert any(entry.changes and entry.changes.get("node_name") for entry in node_entries)

    response = client.get(reverse("settings_audit", args=[secure_id(tenant)]))
    assert response.status_code == 200
    assert response.context["entries"]
    assert b"collector-after" in response.content
    assert b"audit-page-user@example.com" in response.content


@pytest.mark.django_db
def test_node_heartbeat_fields_are_not_audited(billing_plan):
    LogEntry = get_logentry_model()
    tenant = make_tenant("audit-heartbeat", "ah", billing_plan)
    node = make_node(tenant, "heartbeat-node")
    node_content_type = ContentType.objects.get_for_model(Node)
    LogEntry.objects.filter(content_type=node_content_type, object_pk=str(node.pk)).delete()

    node.mark_seen(config_poll=True)

    assert not LogEntry.objects.filter(
        content_type=node_content_type,
        object_pk=str(node.pk),
    ).exists()
