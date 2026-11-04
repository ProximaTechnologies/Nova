import io
from unittest.mock import Mock

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError

from config.models import (
    SupervisorVersion,
    SupervisorVersionArtifact,
    Tenant,
    TenantBilling,
    TenantLimit,
    TenantRole,
    TenantUsage,
    TenantUser,
)

DEBUG_BOOTSTRAP_EMAIL = "admin@domain.com"
DEBUG_BOOTSTRAP_PASSWORD = "P@ssw0rd"
BOOTSTRAP_PASSWORD = "R4ndom-Install-Key-2026!"
OLD_BOOTSTRAP_PASSWORD = "R4ndom-Install-Old-2026!"
NEW_BOOTSTRAP_PASSWORD = "R4ndom-Install-New-2026!"


@pytest.fixture(autouse=True)
def external_side_effects(monkeypatch, settings):
    settings.GRAFANA_URL = "https://grafana.example"
    settings.GRAFANA_OAUTH_CLIENT_NAME = "Grafana OAuth"
    settings.NOVA_OIDC_ISSUER_URL = "https://nova.example"

    def create_tenant_side_effect(_tenant_name, tenant, custom_limit=None):
        tenant.grafana_org_id = 42
        tenant.save(update_fields=["grafana_org_id"])

    monkeypatch.setattr("config.models.create_tenant", Mock(side_effect=create_tenant_side_effect))
    monkeypatch.setattr("config.models.update_grafana_org_mapping", Mock())
    monkeypatch.setattr("config.models.logout_user_from_grafana_using_email", Mock())


@pytest.mark.django_db
def test_bootstrap_install_creates_owner_tenant_membership_billing_and_oidc():
    call_command(
        "bootstrap_install",
        "--email",
        "owner@example.test",
        "--password",
        BOOTSTRAP_PASSWORD,
        "--tenant-name",
        "Homelab",
        "--noinput",
    )

    User = get_user_model()
    user = User.objects.get(email="owner@example.test")
    tenant = Tenant.objects.get(name="Homelab")

    assert user.username == "owner"
    assert user.is_staff is True
    assert user.is_superuser is True
    assert user.check_password(BOOTSTRAP_PASSWORD) is True
    assert tenant.grafana_org_id == 42
    assert TenantBilling.objects.filter(name="Default").exists()
    assert TenantLimit.objects.filter(tenant=tenant, billing__name="Default").exists()
    assert TenantUsage.objects.filter(tenant=tenant).exists()
    assert TenantUser.objects.get(user=user, tenant=tenant).role == TenantRole.ADMIN

    from allauth.account.models import EmailAddress
    from oidc_provider.models import Client, RSAKey

    email_address = EmailAddress.objects.get(user=user, email="owner@example.test")
    assert email_address.verified is True
    assert email_address.primary is True
    assert Client.objects.get(name="Grafana OAuth").client_id
    assert RSAKey.objects.exists()


@pytest.mark.django_db
def test_bootstrap_install_generates_owner_password_for_new_user(settings):
    settings.DEBUG = False
    stdout = io.StringIO()

    call_command(
        "bootstrap_install",
        "--email",
        "owner@example.test",
        "--tenant-name",
        "Homelab",
        "--noinput",
        stdout=stdout,
    )

    password_line = stdout.getvalue().splitlines()[0]
    assert password_line.startswith("Owner password: ")
    generated_password = password_line.removeprefix("Owner password: ")
    assert generated_password
    assert generated_password != DEBUG_BOOTSTRAP_PASSWORD
    assert get_user_model().objects.get(email="owner@example.test").check_password(generated_password) is True


@pytest.mark.django_db
def test_bootstrap_install_uses_debug_owner_defaults(settings, monkeypatch):
    settings.DEBUG = True
    monkeypatch.delenv("NOVA_BOOTSTRAP_EMAIL", raising=False)
    monkeypatch.delenv("NOVA_BOOTSTRAP_PASSWORD", raising=False)

    call_command(
        "bootstrap_install",
        "--tenant-name",
        "Homelab",
        "--noinput",
    )

    user = get_user_model().objects.get(email=DEBUG_BOOTSTRAP_EMAIL)
    assert user.username == "admin"
    assert user.check_password(DEBUG_BOOTSTRAP_PASSWORD) is True


@pytest.mark.django_db
def test_bootstrap_install_without_password_keeps_existing_owner_password():
    call_command(
        "bootstrap_install",
        "--email",
        "owner@example.test",
        "--password",
        BOOTSTRAP_PASSWORD,
        "--tenant-name",
        "Homelab",
        "--noinput",
    )

    call_command(
        "bootstrap_install",
        "--email",
        "owner@example.test",
        "--tenant-name",
        "Homelab",
        "--noinput",
    )

    assert get_user_model().objects.get(email="owner@example.test").check_password(BOOTSTRAP_PASSWORD) is True


@pytest.mark.django_db
def test_bootstrap_install_is_idempotent_and_promotes_existing_membership():
    call_command(
        "bootstrap_install",
        "--email",
        "owner@example.test",
        "--password",
        OLD_BOOTSTRAP_PASSWORD,
        "--tenant-name",
        "Homelab",
        "--noinput",
    )
    user = get_user_model().objects.get(email="owner@example.test")
    tenant = Tenant.objects.get(name="Homelab")
    TenantUser.objects.filter(user=user, tenant=tenant).update(role=TenantRole.VIEWER)

    call_command(
        "bootstrap_install",
        "--email",
        "owner@example.test",
        "--password",
        NEW_BOOTSTRAP_PASSWORD,
        "--tenant-name",
        "Homelab",
        "--noinput",
    )

    user.refresh_from_db()
    assert user.check_password(NEW_BOOTSTRAP_PASSWORD) is True
    assert Tenant.objects.filter(name="Homelab").count() == 1
    assert TenantBilling.objects.filter(name="Default").count() == 1
    assert TenantLimit.objects.filter(tenant=tenant).count() == 1
    assert TenantUsage.objects.filter(tenant=tenant).count() == 1
    assert TenantUser.objects.get(user=user, tenant=tenant).role == TenantRole.ADMIN


@pytest.mark.django_db
def test_bootstrap_install_can_sync_grafana(monkeypatch):
    sync_grafana = Mock()
    monkeypatch.setattr("config.integration.update_grafana_org_mapping", sync_grafana)

    call_command(
        "bootstrap_install",
        "--email",
        "owner@example.test",
        "--password",
        BOOTSTRAP_PASSWORD,
        "--tenant-name",
        "Homelab",
        "--sync-grafana",
        "--noinput",
    )

    sync_grafana.assert_called_once()


@pytest.mark.django_db
def test_bootstrap_install_registers_embedded_supervisor_artifacts(tmp_path):
    (tmp_path / "nova-supervisor-0.4.1-linux-amd64").write_text("amd64", encoding="utf-8")
    (tmp_path / "nova-supervisor-0.4.1-linux-arm64").write_text("arm64", encoding="utf-8")
    (tmp_path / "SHA256SUMS").write_text(
        "\n".join(
            [
                f"{'a' * 64}  nova-supervisor-0.4.1-linux-amd64",
                f"{'b' * 64}  nova-supervisor-0.4.1-linux-arm64",
                f"{'c' * 64}  nova-supervisor-0.3.0-linux-amd64",
            ]
        ),
        encoding="utf-8",
    )

    call_command(
        "bootstrap_install",
        "--email",
        "owner@example.test",
        "--password",
        BOOTSTRAP_PASSWORD,
        "--tenant-name",
        "Homelab",
        "--register-supervisor-version",
        "--supervisor-version",
        "0.4.1",
        "--supervisor-binary-dir",
        str(tmp_path),
        "--noinput",
    )

    release = SupervisorVersion.objects.get(version="0.4.1")
    assert release.enabled is True
    assert set(
        SupervisorVersionArtifact.objects.filter(version=release).values_list("arch", "sha256")
    ) == {
        ("amd64", "a" * 64),
        ("arm64", "b" * 64),
    }


@pytest.mark.django_db
def test_bootstrap_install_skips_non_semver_supervisor_registration():
    call_command(
        "bootstrap_install",
        "--email",
        "owner@example.test",
        "--password",
        BOOTSTRAP_PASSWORD,
        "--tenant-name",
        "Homelab",
        "--register-supervisor-version",
        "--supervisor-version",
        "latest",
        "--noinput",
    )

    assert SupervisorVersion.objects.count() == 0
    assert Tenant.objects.filter(name="Homelab").exists()


@pytest.mark.django_db
def test_bootstrap_install_validates_supplied_owner_password_before_oidc_side_effects(settings):
    settings.DEBUG = False

    from oidc_provider.models import Client

    with pytest.raises(CommandError, match="too common"):
        call_command(
            "bootstrap_install",
            "--email",
            "owner@example.test",
            "--password",
            "password",
            "--tenant-name",
            "Homelab",
            "--noinput",
        )

    assert Client.objects.count() == 0
    assert Tenant.objects.count() == 0
