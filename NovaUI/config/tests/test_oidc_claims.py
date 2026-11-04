from unittest.mock import Mock

import pytest
from django.contrib.auth.models import User

from NovaUI.oidc_provider_settings import CustomScopeClaims, userinfo
from config.models import Tenant, TenantRole, TenantUser
from config.oidc_claims import tenant_claims_for_user


@pytest.fixture(autouse=True)
def external_side_effects(monkeypatch):
    monkeypatch.setattr("config.models.create_tenant", Mock())
    monkeypatch.setattr("config.models.logout_user_from_grafana_using_email", Mock())


def make_scope_claims(user):
    claims = CustomScopeClaims.__new__(CustomScopeClaims)
    claims.user = user
    return claims


@pytest.mark.django_db
def test_userinfo_exposes_stable_grafana_identity_claims():
    user = User.objects.create_user(username="alice", email="alice@example.com", password="password")

    claims = userinfo({}, user)

    assert claims["sub"] == str(user.pk)
    assert claims["login"] == "alice"
    assert claims["preferred_username"] == "alice"
    assert claims["name"] == "alice"
    assert claims["email"] == "alice@example.com"


@pytest.mark.django_db
def test_tenant_claims_for_user_uses_stable_prefix_and_roles():
    user = User.objects.create_user(username="alice", email="alice@example.com", password="password")
    tenant_a = Tenant.objects.create(name="Tenant A", random_prefix="ta", grafana_org_id=10)
    tenant_b = Tenant.objects.create(name="Tenant B", random_prefix="tb", grafana_org_id=11)
    TenantUser.objects.create(user=user, tenant=tenant_a, role=TenantRole.ADMIN)
    TenantUser.objects.create(user=user, tenant=tenant_b, role=TenantRole.VIEWER)

    claims = tenant_claims_for_user(user)

    assert claims == {
        "groups": ["nova-ta-admin", "nova-tb-viewer"],
        "roles": ["admin", "viewer"],
    }
    assert tenant_claims_for_user(user, tenant=tenant_b) == {
        "groups": ["nova-tb-viewer"],
        "roles": ["viewer"],
    }


@pytest.mark.django_db
def test_custom_scope_claims_expose_groups_and_roles():
    user = User.objects.create_user(username="bob", email="bob@example.com", password="password")
    tenant = Tenant.objects.create(name="Tenant", random_prefix="tn", grafana_org_id=12)
    TenantUser.objects.create(user=user, tenant=tenant, role=TenantRole.EDITOR)
    claims = make_scope_claims(user)

    assert claims.scope_groups() == {"groups": ["nova-tn-editor"]}
    assert claims.scope_roles() == {"roles": ["editor"]}
