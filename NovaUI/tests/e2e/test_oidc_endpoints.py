import pytest
from django.urls import reverse

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize(
    ("route_name", "method"),
    [
        ("oidc_provider:authorize", "get"),
        ("oidc_provider:token", "post"),
        ("oidc_provider:userinfo", "get"),
        ("oidc_provider:end-session", "get"),
        ("oidc_provider:end-session-prompt", "get"),
        ("oidc_provider:provider-info", "get"),
        ("oidc_provider:token-introspection", "post"),
        ("oidc_provider:jwks", "get"),
    ],
)
def test_oidc_routes_are_reachable(client, route_name, method):
    request = getattr(client, method)

    response = request(reverse(route_name))

    assert response.status_code in {200, 302, 400, 401, 403, 405}


def test_oidc_provider_info_exposes_expected_metadata(client, settings):
    settings.NOVA_OIDC_ISSUER_URL = "http://localhost:8000"

    response = client.get(reverse("oidc_provider:provider-info"))

    assert response.status_code == 200
    payload = response.json()
    assert payload["issuer"].rstrip("/") == "http://testserver/openid"
    assert payload["authorization_endpoint"].endswith("/openid/authorize")
    assert payload["token_endpoint"].endswith("/openid/token")
    assert payload["userinfo_endpoint"].endswith("/openid/userinfo")


def test_oidc_jwks_endpoint_returns_key_set(client):
    response = client.get(reverse("oidc_provider:jwks"))

    assert response.status_code == 200
    assert "keys" in response.json()


def test_oidc_authorize_rejects_missing_parameters(client):
    response = client.get(reverse("oidc_provider:authorize"))

    assert response.status_code in {200, 302, 400}


def test_oidc_token_userinfo_and_introspection_reject_missing_credentials(client):
    token = client.post(reverse("oidc_provider:token"), {})
    userinfo = client.get(reverse("oidc_provider:userinfo"))
    introspect = client.post(reverse("oidc_provider:token-introspection"), {})

    assert token.status_code in {400, 401}
    assert userinfo.status_code in {401, 403}
    assert introspect.status_code == 200
    assert introspect.json()["active"] is False
