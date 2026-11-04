from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from config.integration import update_grafana_org_mapping
from config.models import Tenant
from config.oidc_bootstrap import bootstrap_grafana_oidc_client


@pytest.fixture(autouse=True)
def external_side_effects(monkeypatch):
    monkeypatch.setattr("config.models.create_tenant", Mock())


def configure_grafana_settings(settings):
    settings.GRAFANA_PROTOCOL = "http"
    settings.GRAFANA_HOST = "grafana.monitoring.svc.cluster.local:3000"
    settings.GRAFANA_USER = "admin"
    settings.GRAFANA_PASSWORD = "password"
    settings.GRAFANA_URL = "http://grafana.monitoring.svc.cluster.local:3000"
    settings.GRAFANA_EXTERNAL_URL = "https://grafana.example"
    settings.GRAFANA_OAUTH_CLIENT_NAME = "Grafana OAuth"
    settings.NOVA_OIDC_ISSUER_URL = "https://nova.example/"


@pytest.mark.django_db
def test_update_grafana_org_mapping_sends_safe_role_config(monkeypatch, settings):
    put = Mock(return_value=SimpleNamespace(status_code=204))
    monkeypatch.setattr("config.integration.requests.put", put)
    configure_grafana_settings(settings)
    settings.GRAFANA_OAUTH_TLS_SKIP_VERIFY_INSECURE = False
    bootstrap_grafana_oidc_client(client_id="client-id", client_secret="client-secret")
    Tenant.objects.create(name="Tenant", random_prefix="tn", grafana_org_id=15)

    update_grafana_org_mapping()

    put.assert_called_once()
    assert put.call_args.args[0] == (
        "http://grafana.monitoring.svc.cluster.local:3000/api/v1/sso-settings/generic_oauth"
    )
    assert put.call_args.kwargs["auth"] == ("admin", "password")

    payload_settings = put.call_args.kwargs["json"]["settings"]
    assert payload_settings["apiUrl"] == "https://nova.example/openid/userinfo"
    assert payload_settings["authUrl"] == "https://nova.example/openid/authorize"
    assert payload_settings["tokenUrl"] == "https://nova.example/openid/token"
    assert payload_settings["clientId"] == "client-id"
    assert payload_settings["clientSecret"] == "client-secret"
    assert payload_settings["allowAssignGrafanaAdmin"] == "false"
    assert payload_settings["roleAttributePath"] == "'Viewer'"
    assert payload_settings["roleAttributeStrict"] == "true"
    assert payload_settings["scopes"] == "[\"openid\",\"profile\",\"email\",\"groups\",\"roles\"]"
    assert payload_settings["loginAttributePath"] == "sub"
    assert payload_settings["emailAttributePath"] == "email"
    assert payload_settings["orgAttributePath"] == "groups"
    assert payload_settings["tlsSkipVerifyInsecure"] == "false"
    assert payload_settings["orgMapping"] == [
        "nova-tn-admin:15:Editor",
        "nova-tn-editor:15:Editor",
        "nova-tn-viewer:15:Viewer",
    ]
    assert all(not mapping.endswith(":Admin") for mapping in payload_settings["orgMapping"])


@pytest.mark.django_db
def test_update_grafana_org_mapping_can_skip_oauth_tls_verify(monkeypatch, settings):
    put = Mock(return_value=SimpleNamespace(status_code=204))
    monkeypatch.setattr("config.integration.requests.put", put)
    configure_grafana_settings(settings)
    settings.GRAFANA_OAUTH_TLS_SKIP_VERIFY_INSECURE = True
    bootstrap_grafana_oidc_client(client_id="client-id", client_secret="client-secret")

    update_grafana_org_mapping()

    payload_settings = put.call_args.kwargs["json"]["settings"]
    assert payload_settings["tlsSkipVerifyInsecure"] == "true"
