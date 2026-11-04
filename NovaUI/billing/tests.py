from datetime import timedelta
from io import StringIO
from unittest.mock import Mock

import pytest
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.urls import reverse
from django.utils import timezone

from billing.models import Billing, InvoiceStatus
from config.models import Node, TenantBillingStatus
from config.tests.test_v1_security import make_tenant, make_user, make_member, billing_plan


@pytest.mark.django_db
@pytest.mark.parametrize("enabled", [False, True])
def test_optional_billing(settings, monkeypatch, client, billing_plan, enabled):
    settings.BILLING_ENABLED = enabled
    for name in ("create_tenant", "delete_tenant", "update_grafana_org_mapping", "logout_user_from_grafana_using_email"):
        monkeypatch.setattr(f"config.models.{name}", Mock())
    tenant = make_tenant("billing-mode", "bm", billing_plan,
                         billing_status=TenantBillingStatus.SUSPENDED)
    node = Node.objects.create(tenant=tenant, node_name="collector")
    assert tenant.billing_suspended is enabled
    if enabled:
        with pytest.raises(ValidationError):
            node.generate_token()
    else:
        node.generate_token().validate_usable()
    bill = Billing.objects.create(tenant=tenant,
                                  due_date=timezone.now().date() - timedelta(days=31))
    call_command("update_invoice_statuses", stdout=StringIO())
    bill.refresh_from_db()
    assert bill.status == (InvoiceStatus.OVERDUE if enabled else InvoiceStatus.AWAITING_PAYMENT)
    bill.status = InvoiceStatus.PAID
    bill.save(update_fields=["status"])
    call_command("update_invoice_statuses", stdout=StringIO())
    tenant.refresh_from_db()
    assert tenant.billing_status == (TenantBillingStatus.ACTIVE if enabled else TenantBillingStatus.SUSPENDED)
    user = make_user("billing-admin")
    make_member(user, tenant)
    client.force_login(user)
    url = reverse("settings_billing", args=[f"{tenant.random_prefix}-{tenant.name}"])
    response = client.get(url)
    assert response.status_code == (200 if enabled else 404)
    dashboard = client.get(reverse("dashboard", args=[f"{tenant.random_prefix}-{tenant.name}"]))
    assert (f'href="{url}"'.encode() in dashboard.content) is enabled

    user.is_staff = user.is_superuser = True
    user.save(update_fields=["is_staff", "is_superuser"])
    assert client.get(reverse("admin:billing_billing_changelist")).status_code == (200 if enabled else 403)

    from agent.models import CollectorAuth
    from config.gitops.plugins.apisix import APISIXPlugin

    Tenant = type(tenant)
    Tenant.objects.filter(pk=tenant.pk).update(billing_status=TenantBillingStatus.SUSPENDED)
    CollectorAuth.objects.create(node=node, config_consumer_name="collector", config_password="test")
    monkeypatch.setattr("config.gitops.plugins.apisix.seal_secret_data", lambda **kwargs: {"password": "sealed"})
    resources = list(APISIXPlugin().desired_resources(tenant=tenant))
    assert any(resource.kind == "ApisixConsumer" for resource in resources) is (not enabled)
