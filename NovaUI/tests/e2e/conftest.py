from itertools import count
from unittest.mock import Mock

import pytest
from channels.layers import channel_layers
from django.contrib.auth.models import User
from django.core.cache import caches

from agent.models import CollectorAuth
from billing.models import Billing, BillingInvoice, BillingReceipt, InvoiceStatus
from config.models import (
    AuditLog,
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

PASSWORD = "password"


@pytest.fixture(autouse=True)
def e2e_external_boundaries(monkeypatch, settings):
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    settings.PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
    settings.CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        },
    }
    caches.close_all()
    settings.ALLOWED_HOSTS = list({*settings.ALLOWED_HOSTS, "testserver", "localhost"})
    settings.CONFIGURATION_URL = "https://config.example.test"
    settings.UI_URL = "http://localhost:8000"
    settings.GRAFANA_URL = "http://grafana.example.test"
    settings.CHANNEL_LAYERS = {
        "default": {
            "BACKEND": "channels.layers.InMemoryChannelLayer",
        },
    }
    channel_layers.backends = {}

    monkeypatch.setattr("config.models.create_tenant", Mock())
    monkeypatch.setattr("config.models.delete_tenant", Mock())
    monkeypatch.setattr("config.models.update_grafana_org_mapping", Mock())
    monkeypatch.setattr("config.models.logout_user_from_grafana_using_email", Mock())
    monkeypatch.setattr("config.models.NodeFeatures._push_to_redis", Mock())
    monkeypatch.setattr("agent.views.NodeFeatures._push_to_redis", Mock())

    pdf = b"%PDF-1.4\n% NovaUI e2e fixture\n"
    monkeypatch.setattr("billing.models.Billing.generate_invoice_pdf", lambda self: pdf)
    monkeypatch.setattr("billing.models.Billing.generate_receipt_pdf", lambda self: pdf)


@pytest.fixture
def user_factory(db):
    seq = count(1)

    def make(username=None, *, password=None, email=None, is_staff=False, is_superuser=False):
        index = next(seq)
        username = username or f"user-{index}"
        email = email or f"{username}@example.test"
        password = PASSWORD if password is None else password
        if is_superuser:
            user = User.objects.create_superuser(username=username, email=email, password=password)
        else:
            user = User.objects.create_user(username=username, email=email, password=password)
            changed = False
            if is_staff:
                user.is_staff = True
                changed = True
            if changed:
                user.save(update_fields=["is_staff"])
        return user

    return make


@pytest.fixture
def billing_plan(db):
    return TenantBilling.objects.create(
        name="Default",
        price_per_node=400,
        price_per_cluster=1500,
        price_per_vcpu=100,
    )


@pytest.fixture
def tenant_factory(db, billing_plan):
    seq = count(1)

    def make(
            name=None,
            *,
            prefix=None,
            billing_status=TenantBillingStatus.ACTIVE,
            max_nodes=5,
            max_clusters=2,
            max_vcpus=16,
            usage_nodes=0,
            usage_clusters=0,
            usage_vcpus=0,
            with_limit=True,
            with_usage=True,
            grafana_org_id=None,
    ):
        index = next(seq)
        name = name or f"tenant-{index}"
        prefix = prefix or f"t{index}"
        tenant = Tenant.objects.create(
            name=name,
            random_prefix=prefix,
            grafana_org_id=grafana_org_id if grafana_org_id is not None else 100 + index,
            billing_status=billing_status,
        )
        if with_limit:
            TenantLimit.objects.create(
                tenant=tenant,
                billing=billing_plan,
                max_nodes=max_nodes,
                max_clusters=max_clusters,
                max_vcpus=max_vcpus,
            )
        if with_usage:
            TenantUsage.objects.create(
                tenant=tenant,
                nodes=usage_nodes,
                clusters=usage_clusters,
                vcpus=usage_vcpus,
            )
        return tenant

    return make


@pytest.fixture
def member_factory(db):
    def make(user, tenant, role=TenantRole.ADMIN):
        return TenantUser.objects.create(user=user, tenant=tenant, role=role)

    return make


@pytest.fixture
def node_factory(db):
    def make(tenant, name="collector", *, features=True, feature_kwargs=None, **node_kwargs):
        node = Node.objects.create(tenant=tenant, node_name=name, **node_kwargs)
        if features:
            values = {"linuxMonitor": True}
            values.update(feature_kwargs or {})
            NodeFeatures.objects.create(node=node, **values)
        return node

    return make


@pytest.fixture
def billing_factory(db, tenant_factory):
    def make(tenant=None, *, status=InvoiceStatus.AWAITING_PAYMENT):
        tenant = tenant or tenant_factory()
        return Billing.objects.create(
            tenant=tenant,
            status=status,
            nodes=1,
            clusters=1,
            vcpus=1,
            currency="USD",
        )

    return make


@pytest.fixture
def invoice_factory(db, billing_factory):
    def make(billing=None):
        billing = billing or billing_factory()
        return BillingInvoice.objects.create(
            billing=billing,
            invoice_number="INV-202605001",
            invoice_month="202605",
            invoice_sequence=1,
            pdf_filename="INV-202605001.pdf",
            pdf=b"%PDF invoice\n",
        )

    return make


@pytest.fixture
def receipt_factory(db, billing_factory):
    def make(billing=None):
        billing = billing or billing_factory(status=InvoiceStatus.PAID)
        return BillingReceipt.objects.create(
            billing=billing,
            pdf_filename="Receipt-INV-202605001.pdf",
            pdf=b"%PDF receipt\n",
        )

    return make


def secure_id(tenant):
    return f"{tenant.random_prefix}-{tenant.name}"


def login(client, user):
    client.force_login(user)
    return user


@pytest.fixture
def secure_tenant_id():
    return secure_id


@pytest.fixture
def login_user():
    return login


@pytest.fixture
def password():
    return PASSWORD


__all__ = [
    "AuditLog",
    "CollectorAuth",
    "Node",
    "NodeFeatures",
    "NodeToken",
    "PASSWORD",
    "TenantBillingStatus",
    "TenantRole",
    "TenantUsage",
    "login",
    "secure_id",
]


@pytest.fixture(autouse=True)
def enable_optional_billing(settings):
    settings.BILLING_ENABLED = True
