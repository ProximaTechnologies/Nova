from types import SimpleNamespace

import pytest
from django.urls import reverse

from billing.models import BillingInvoice, BillingReceipt, InvoiceStatus

pytestmark = pytest.mark.django_db

ADMIN_MODEL_PREFIXES = [
    "auth_group",
    "auth_user",
    "oidc_provider_client",
    "oidc_provider_code",
    "oidc_provider_token",
    "oidc_provider_rsakey",
    "account_emailaddress",
    "socialaccount_socialapp",
    "socialaccount_socialtoken",
    "socialaccount_socialaccount",
    "django_celery_results_taskresult",
    "django_celery_results_groupresult",
    "config_tenant",
    "config_tenantbilling",
    "config_tenantlimit",
    "config_tenantuser",
    "config_node",
    "config_nodetoken",
    "config_tenantusage",
    "config_auditlog",
    "agent_collectorauth",
    "billing_billing",
    "billing_billinginvoice",
    "billing_billingreceipt",
]


@pytest.fixture
def superuser(user_factory):
    return user_factory("admin-e2e", is_superuser=True)


def test_admin_index_redirects_anonymous_and_allows_superuser(client, superuser, login_user):
    anonymous = client.get(reverse("admin:index"))
    assert anonymous.status_code == 302
    assert reverse("admin:login") in anonymous["Location"]

    login_user(client, superuser)
    response = client.get(reverse("admin:index"))
    assert response.status_code == 200
    assert b"Site administration" in response.content


@pytest.mark.parametrize(
    ("route_name", "args", "query"),
    [
        ("admin:login", [], {}),
        ("admin:logout", [], {}),
        ("admin:password_change", [], {}),
        ("admin:password_change_done", [], {}),
        ("admin:jsi18n", [], {}),
        ("admin:autocomplete", [], {}),
        ("admin:view_on_site", ["999999", "1"], {}),
        ("admin:app_list", ["config"], {}),
    ],
)
def test_admin_site_level_routes_are_reachable_for_superuser(
        client,
        superuser,
        login_user,
        route_name,
        args,
        query,
):
    login_user(client, superuser)

    response = client.get(reverse(route_name, args=args), query)

    assert response.status_code in {200, 302, 403, 404, 405}


@pytest.mark.parametrize("model_prefix", ADMIN_MODEL_PREFIXES)
def test_admin_model_changelist_and_add_routes_are_reachable_for_superuser(
        client,
        superuser,
        login_user,
        model_prefix,
):
    login_user(client, superuser)

    changelist = client.get(reverse(f"admin:{model_prefix}_changelist"))
    add = client.get(reverse(f"admin:{model_prefix}_add"))

    assert changelist.status_code == 200
    assert add.status_code in {200, 302, 403}


@pytest.mark.parametrize("model_prefix", ADMIN_MODEL_PREFIXES)
@pytest.mark.parametrize("suffix", ["history", "change", "delete"])
def test_admin_model_object_routes_exist_for_superuser(
        client,
        superuser,
        login_user,
        model_prefix,
        suffix,
):
    login_user(client, superuser)

    response = client.get(reverse(f"admin:{model_prefix}_{suffix}", args=["999999"]))

    assert response.status_code in {200, 302, 404}


def test_admin_auth_user_password_change_route_exists_for_superuser(client, superuser, login_user):
    login_user(client, superuser)

    response = client.get(reverse("admin:auth_user_password_change", args=[superuser.pk]))

    assert response.status_code == 200


def test_billing_invoice_and_receipt_pdf_admin_downloads(
        client,
        superuser,
        login_user,
        invoice_factory,
        receipt_factory,
):
    invoice = invoice_factory()
    receipt = receipt_factory()
    login_user(client, superuser)

    invoice_response = client.get(reverse("admin:billing_invoice_pdf", args=[invoice.pk]))
    receipt_response = client.get(reverse("admin:billing_receipt_pdf", args=[receipt.pk]))

    assert invoice_response.status_code == 200
    assert invoice_response["Content-Type"] == "application/pdf"
    assert invoice.pdf_filename in invoice_response["Content-Disposition"]
    assert receipt_response.status_code == 200
    assert receipt_response["Content-Type"] == "application/pdf"
    assert receipt.pdf_filename in receipt_response["Content-Disposition"]

    assert client.get(reverse("admin:billing_invoice_pdf", args=[999999])).status_code == 404
    assert client.get(reverse("admin:billing_receipt_pdf", args=[999999])).status_code == 404


def test_tenant_admin_grafana_sync_action(
        client,
        superuser,
        login_user,
        tenant_factory,
        monkeypatch,
):
    tenant_a = tenant_factory("admin-grafana-a", prefix="aga")
    tenant_b = tenant_factory("admin-grafana-b", prefix="agb")
    calls = []

    def fake_sync_gitops_resources(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(rendered_count=9, synced_count=9)

    monkeypatch.setattr("config.gitops.sync.sync_gitops_resources", fake_sync_gitops_resources)
    login_user(client, superuser)

    response = client.post(
        reverse("admin:config_tenant_changelist"),
        {
            "action": "sync_grafana_gitops",
            "_selected_action": [str(tenant_a.pk), str(tenant_b.pk)],
            "index": "0",
        },
    )

    assert response.status_code == 302
    assert {
        (call["plugin_names"][0], call["tenant_id"], call["reason"], call["actor"])
        for call in calls
    } == {
        ("grafana", tenant_a.pk, "admin_grafana_sync", superuser),
        ("grafana", tenant_b.pk, "admin_grafana_sync", superuser),
    }


@pytest.mark.parametrize(
    ("action", "expected_status"),
    [
        ("mark_draft", InvoiceStatus.DRAFT),
        ("mark_awaiting_payment", InvoiceStatus.AWAITING_PAYMENT),
        ("mark_paid", InvoiceStatus.PAID),
        ("mark_overdue", InvoiceStatus.OVERDUE),
        ("mark_void", InvoiceStatus.VOID),
    ],
)
def test_billing_admin_status_actions_update_invoice_state(
        client,
        superuser,
        login_user,
        billing_factory,
        action,
        expected_status,
):
    billing = billing_factory()
    login_user(client, superuser)

    response = client.post(
        reverse("admin:billing_billing_changelist"),
        {
            "action": action,
            "_selected_action": [str(billing.pk)],
            "index": "0",
        },
    )

    assert response.status_code == 302
    billing.refresh_from_db()
    assert billing.status == expected_status


def test_billing_admin_pdf_generation_actions_create_artifacts(
        client,
        superuser,
        login_user,
        billing_factory,
):
    invoice_billing = billing_factory()
    receipt_billing = billing_factory(status=InvoiceStatus.PAID)
    login_user(client, superuser)

    invoice_response = client.post(
        reverse("admin:billing_billing_changelist"),
        {
            "action": "generate_invoice_pdf",
            "_selected_action": [str(invoice_billing.pk)],
            "index": "0",
        },
    )
    receipt_response = client.post(
        reverse("admin:billing_billing_changelist"),
        {
            "action": "generate_receipt_pdf",
            "_selected_action": [str(receipt_billing.pk)],
            "index": "0",
        },
    )

    assert invoice_response.status_code == 302
    assert receipt_response.status_code == 302
    assert BillingInvoice.objects.filter(billing=invoice_billing).exists()
    assert BillingReceipt.objects.filter(billing=receipt_billing).exists()
