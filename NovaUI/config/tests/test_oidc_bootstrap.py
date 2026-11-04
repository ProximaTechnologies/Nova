from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from cryptography.hazmat.primitives import serialization
from django.core.management import call_command

from config.integration import update_grafana_org_mapping
from config.models import Tenant
from config.oidc_bootstrap import bootstrap_grafana_oidc_client


@pytest.fixture(autouse=True)
def external_side_effects(monkeypatch):
    monkeypatch.setattr("config.models.create_tenant", Mock())
    monkeypatch.setattr("config.models.update_grafana_org_mapping", Mock())


@pytest.mark.django_db
def test_bootstrap_grafana_oidc_client_creates_client_and_rsa_key(settings):
    from oidc_provider.models import Client, RSAKey

    settings.GRAFANA_URL = "http://grafana.monitoring.svc.cluster.local:3000"
    settings.GRAFANA_EXTERNAL_URL = "https://grafana.example"
    settings.GRAFANA_OAUTH_CLIENT_NAME = "Grafana OAuth"

    result = bootstrap_grafana_oidc_client()

    client = Client.objects.get(pk=result.client.pk)
    assert result.client_created is True
    assert result.rsa_key_created is True
    assert client.name == "Grafana OAuth"
    assert client.client_type == "confidential"
    assert client.jwt_alg == "RS256"
    assert client.require_consent is False
    assert client.redirect_uris == ["https://grafana.example/login/generic_oauth"]
    assert client.website_url == "https://grafana.example"
    assert client.scope == ["openid", "profile", "email", "groups", "roles"]
    assert list(client.response_types.values_list("value", flat=True)) == ["code"]
    assert client.client_id
    assert client.client_secret

    key = RSAKey.objects.get(pk=result.rsa_key.pk)
    serialization.load_pem_private_key(key.key.encode("utf-8"), password=None)


@pytest.mark.django_db
def test_bootstrap_grafana_oidc_client_is_idempotent(settings):
    from oidc_provider.models import Client, RSAKey

    settings.GRAFANA_URL = "https://grafana.example"
    settings.GRAFANA_OAUTH_CLIENT_NAME = "Grafana OAuth"

    first = bootstrap_grafana_oidc_client()
    first_client_id = first.client.client_id
    first_client_secret = first.client.client_secret
    first_key_id = first.rsa_key.pk

    second = bootstrap_grafana_oidc_client()

    assert second.client_created is False
    assert second.rsa_key_created is False
    assert second.client.pk == first.client.pk
    assert second.client.client_id == first_client_id
    assert second.client.client_secret == first_client_secret
    assert second.rsa_key.pk == first_key_id
    assert Client.objects.count() == 1
    assert RSAKey.objects.count() == 1


@pytest.mark.django_db
def test_bootstrap_grafana_oidc_command_can_sync_grafana(monkeypatch, settings):
    put = Mock(return_value=SimpleNamespace(status_code=204))
    monkeypatch.setattr("config.integration.requests.put", put)
    settings.GRAFANA_URL = "https://grafana.example"
    settings.GRAFANA_PROTOCOL = "https"
    settings.GRAFANA_HOST = "grafana.example"
    settings.GRAFANA_USER = "admin"
    settings.GRAFANA_PASSWORD = "password"
    settings.GRAFANA_OAUTH_CLIENT_NAME = "Grafana OAuth"
    settings.NOVA_OIDC_ISSUER_URL = "https://nova.example/"
    Tenant.objects.create(name="Tenant", random_prefix="tn", grafana_org_id=15)

    call_command("bootstrap_grafana_oidc", "--sync-grafana")

    put.assert_called_once()
    from oidc_provider.models import Client

    client = Client.objects.get(name="Grafana OAuth")
    payload_settings = put.call_args.kwargs["json"]["settings"]
    assert payload_settings["clientId"] == client.client_id
    assert payload_settings["clientSecret"] == client.client_secret
    assert payload_settings["clientSecret"]
    assert payload_settings["orgMapping"] == [
        "nova-tn-admin:15:Editor",
        "nova-tn-editor:15:Editor",
        "nova-tn-viewer:15:Viewer",
    ]


@pytest.mark.django_db
def test_update_grafana_org_mapping_uses_bootstrapped_client(monkeypatch, settings):
    put = Mock(return_value=SimpleNamespace(status_code=204))
    monkeypatch.setattr("config.integration.requests.put", put)
    settings.GRAFANA_URL = "https://grafana.example"
    settings.GRAFANA_PROTOCOL = "https"
    settings.GRAFANA_HOST = "grafana.example"
    settings.GRAFANA_USER = "admin"
    settings.GRAFANA_PASSWORD = "password"
    settings.GRAFANA_OAUTH_CLIENT_NAME = "Grafana OAuth"
    settings.NOVA_OIDC_ISSUER_URL = "https://nova.example/"
    result = bootstrap_grafana_oidc_client()

    update_grafana_org_mapping()

    payload_settings = put.call_args.kwargs["json"]["settings"]
    assert payload_settings["clientId"] == result.client.client_id
    assert payload_settings["clientSecret"] == result.client.client_secret
