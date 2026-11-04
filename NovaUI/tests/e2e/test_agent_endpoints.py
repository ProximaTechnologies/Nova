import base64
import hashlib
from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from agent.models import CollectorAuth
from config.models import (
    AlloyVersion,
    AlloyVersionArtifact,
    AuditLog,
    Node,
    NodeFeatures,
    NodeToken,
    SupervisorVersion,
    SupervisorVersionArtifact,
    TenantBillingStatus,
)

pytestmark = pytest.mark.django_db


def config_auth_header(auth):
    credentials = f"{auth.config_consumer_name}:{auth.config_password}".encode("utf-8")
    return "Basic " + base64.b64encode(credentials).decode("ascii")


def mimir_auth_header(auth):
    credentials = f"{auth.mimir_consumer_name}:{auth.mimir_password}".encode("utf-8")
    return "Basic " + base64.b64encode(credentials).decode("ascii")


def test_installer_consumes_token_once_creates_credentials_and_returns_shell_script(
        client,
        user_factory,
        tenant_factory,
        member_factory,
        node_factory,
):
    user = user_factory("installer-user")
    tenant = tenant_factory("installer-tenant", prefix="it")
    member_factory(user, tenant)
    node = node_factory(tenant, "installer-node")
    token = node.generate_token(actor=user)

    response = client.get(reverse("installer", args=[token.token]))

    assert response.status_code == 200
    assert response["Content-Type"] == "text/x-sh; charset=utf-8"
    assert response["Content-Disposition"] == 'inline; filename="install.sh"'
    assert response["X-Content-Type-Options"] == "nosniff"
    assert response["Cache-Control"] == "no-store"
    assert b'url = "https://config.example.test"' in response.content
    script = response.content.decode("utf-8")
    assert f'NODE_ID="{node.node_id.hex}"' in script
    assert 'VERSION_REPORT_BASE_URL="http://localhost:8000"' in script
    assert 'INSECURE_TLS="${NOVA_INSECURE_TLS:-0}"' in script
    assert "--insecure|--no-tls-verify)" in script
    assert '"${curl_tls_opts[@]}" --retry 3' in script
    assert 'local version_report_url="${base_url}/agent/version/${NODE_ID}/${ALLOY_VERSION}"' in script
    assert '-X POST "$version_report_url"' in script
    assert 'SUPERVISOR_BIN="/usr/local/bin/nova-supervisor"' in script
    assert 'SUPERVISOR_URL="${NOVA_SUPERVISOR_BINARY_URL:-}"' in script
    assert 'SUPERVISOR_URL="${VERSION_REPORT_BASE_URL%/}/agent/supervisor/binary/{version}/{arch}"' in script
    assert '[[ -n "$SUPERVISOR_SHA256" ]] || err "NOVA_SUPERVISOR_BINARY_SHA256 is required"' in script
    assert 'need sha256sum' in script
    assert 'sha256sum -c - || err "Nova Supervisor checksum mismatch"' in script
    assert "install_supervisor" in script
    assert '"$SUPERVISOR_BIN" bootstrap \\' in script
    assert '-nova-ui-url "$VERSION_REPORT_BASE_URL"' in script
    assert '-node-id "$NODE_ID"' in script
    assert "EnvironmentFile=" not in script
    assert 'ExecStart=${SUPERVISOR_BIN}' in script

    token.refresh_from_db()
    assert token.used_at is not None
    auth = CollectorAuth.objects.get(node=node)
    assert auth.config_consumer_name == f"{node.node_id.hex}-config"
    assert auth.mimir_consumer_name == f"{node.node_id.hex}-mimir"
    assert auth.loki_consumer_name == f"{node.node_id.hex}-loki"
    assert auth.config_password
    assert AuditLog.objects.filter(action="node_token.used", target_id=str(token.pk)).exists()

    second = client.get(reverse("installer", args=[token.token]))
    assert second.status_code == 410
    assert AuditLog.objects.filter(action="node_token.rejected", target_id=str(token.pk)).exists()


def test_installer_can_default_to_insecure_tls_from_settings(client, settings, tenant_factory, node_factory):
    settings.NOVA_AGENT_INSECURE_TLS = True
    tenant = tenant_factory("installer-insecure", prefix="ii")
    node = node_factory(tenant, "installer-insecure-node")
    token = node.generate_token()

    response = client.get(reverse("installer", args=[token.token]))

    assert response.status_code == 200
    script = response.content.decode("utf-8")
    assert 'INSECURE_TLS="${NOVA_INSECURE_TLS:-1}"' in script


def test_installer_repairs_existing_blank_collector_auth(
        client,
        tenant_factory,
        node_factory,
):
    tenant = tenant_factory("installer-repair", prefix="ir")
    node = node_factory(tenant, "repair-node")
    auth = CollectorAuth.objects.create(node=node)
    token = node.generate_token()

    response = client.get(reverse("installer", args=[token.token]))

    assert response.status_code == 200
    auth.refresh_from_db()
    assert auth.config_consumer_name == f"{node.node_id.hex}-config"
    assert auth.config_password
    assert AuditLog.objects.filter(
        action="node_token.used",
        target_id=str(token.pk),
        metadata__repaired_credentials=True,
    ).exists()


@pytest.mark.parametrize("token_state", ["expired", "revoked", "disabled-node", "suspended-tenant"])
def test_installer_rejects_unusable_tokens(client, tenant_factory, node_factory, token_state):
    tenant = tenant_factory("installer-reject", prefix=f"rej{token_state[0]}")
    node = node_factory(tenant, f"reject-{token_state}")
    token = node.generate_token()

    if token_state == "expired":
        token.expires_at = timezone.now() - timedelta(minutes=1)
        token.save(update_fields=["expires_at"])
    elif token_state == "revoked":
        token.revoked_at = timezone.now()
        token.save(update_fields=["revoked_at"])
    elif token_state == "disabled-node":
        node.enabled = False
        node.save(update_fields=["enabled"])
    elif token_state == "suspended-tenant":
        tenant.billing_status = TenantBillingStatus.SUSPENDED
        tenant.save(update_fields=["billing_status"])

    response = client.get(reverse("installer", args=[token.token]))

    assert response.status_code == 410
    assert not CollectorAuth.objects.filter(node=node).exists()
    assert AuditLog.objects.filter(action="node_token.rejected", target_id=str(token.pk)).exists()


def test_installer_missing_token_returns_404(client):
    response = client.get(reverse("installer", args=["missing-token"]))

    assert response.status_code == 404


def test_installer_creates_missing_node_features_for_legacy_node(client, tenant_factory):
    tenant = tenant_factory("installer-legacy", prefix="il")
    node = Node.objects.create(tenant=tenant, node_name="legacy-node")
    token = node.generate_token()

    response = client.get(reverse("installer", args=[token.token]))

    assert response.status_code == 200
    assert NodeFeatures.objects.filter(node=node).exists()


def test_check_in_get_post_and_slash_variant_mark_node_seen(client, tenant_factory, node_factory):
    tenant = tenant_factory("check-in", prefix="ci")
    node = node_factory(tenant, "check-in-node")
    auth = CollectorAuth.objects.create(
        node=node,
        config_consumer_name=f"{node.node_id.hex}-config",
        config_password="secret-config",
        mimir_consumer_name=f"{node.node_id.hex}-mimir",
        mimir_password="secret-mimir",
    )
    auth_header = mimir_auth_header(auth)

    get_response = client.get(reverse("agent_check_in", args=[node.node_id.hex, "v1.10.2"]))
    assert get_response.status_code == 405

    unauthenticated = client.post(reverse("agent_check_in", args=[node.node_id.hex, "v1.10.2"]))
    assert unauthenticated.status_code == 401

    post_response = client.post(
        reverse("agent_check_in", args=[node.node_id.hex, "v1.10.2"]),
        HTTP_AUTHORIZATION=auth_header,
    )
    assert post_response.status_code == 200
    assert post_response.json()["ok"] is True
    assert post_response.json()["node_id"] == node.node_id.hex
    node.refresh_from_db()
    assert node.last_seen_at is not None
    assert node.alloy_version == "v1.10.2"

    post_response = client.post(
        reverse("agent_check_in_slash", args=[node.node_id.hex, "1.11.0"]),
        HTTP_AUTHORIZATION=auth_header,
    )
    assert post_response.status_code == 200
    node.refresh_from_db()
    assert node.alloy_version == "v1.11.0"


def test_check_in_rejects_invalid_and_unknown_nodes(client):
    invalid = client.post(reverse("agent_check_in", args=["not-a-uuid", "v1.10.2"]))
    assert invalid.status_code == 404

    unknown = client.post(reverse("agent_check_in", args=["00000000000000000000000000000000", "v1.10.2"]))
    assert unknown.status_code == 404


def test_check_in_truncates_alloy_version_and_reports_disabled_status(client, tenant_factory, node_factory):
    tenant = tenant_factory("check-in-disabled", prefix="cid")
    node = node_factory(tenant, "disabled-check-in", enabled=False)
    auth = CollectorAuth.objects.create(
        node=node,
        config_consumer_name=f"{node.node_id.hex}-config",
        config_password="secret-config",
        mimir_consumer_name=f"{node.node_id.hex}-mimir",
        mimir_password="secret-mimir",
    )
    long_version = "v" + ("1" * 100)

    response = client.post(
        reverse("agent_check_in", args=[node.node_id.hex, long_version]),
        HTTP_AUTHORIZATION=mimir_auth_header(auth),
    )

    assert response.status_code == 200
    assert response.json()["status"] == "disabled"
    node.refresh_from_db()
    assert len(node.alloy_version) == 50


def test_version_report_records_alloy_version_without_marking_node_seen(client, tenant_factory, node_factory):
    tenant = tenant_factory("version-report", prefix="vr")
    node = node_factory(tenant, "version-report-node")
    auth = CollectorAuth.objects.create(
        node=node,
        config_consumer_name=f"{node.node_id.hex}-config",
        config_password="secret-config",
    )

    response = client.post(
        reverse("agent_report_version", args=[node.node_id.hex, "1.10.2"]),
        HTTP_AUTHORIZATION=config_auth_header(auth),
    )

    assert response.status_code == 200
    assert response.json()["version"] == "v1.10.2"
    assert response.json()["alloy_version"] == "v1.10.2"
    node.refresh_from_db()
    assert node.alloy_version == "v1.10.2"
    assert node.last_seen_at is None


def test_supervisor_version_report_records_version_without_marking_node_seen(client, tenant_factory, node_factory):
    tenant = tenant_factory("supervisor-version-report", prefix="svr")
    node = node_factory(tenant, "supervisor-version-report-node")
    auth = CollectorAuth.objects.create(
        node=node,
        config_consumer_name=f"{node.node_id.hex}-config",
        config_password="secret-config",
    )

    response = client.post(
        reverse("agent_report_supervisor_version", args=[node.node_id.hex, "0.2.0"]),
        {"supervisor_arch": "amd64"},
        HTTP_AUTHORIZATION=config_auth_header(auth),
    )

    assert response.status_code == 200
    assert response.json()["version"] == "0.2.0"
    assert response.json()["supervisor_version"] == "0.2.0"
    assert response.json()["supervisor_arch"] == "amd64"
    node.refresh_from_db()
    assert node.supervisor_version == "0.2.0"
    assert node.supervisor_arch == "amd64"
    assert node.last_seen_at is None


def test_supervisor_bootstrap_returns_runtime_from_novaui(client, settings, tenant_factory, node_factory):
    settings.UI_URL = "https://ui.example.test"
    settings.NOVA_SUPERVISOR_ALLOW_ACTIONS = True
    settings.NOVA_SUPERVISOR_ALLOW_SELF_UPDATE = True
    settings.NOVA_SUPERVISOR_ALLOW_ALLOY_UPDATE = True
    settings.NOVA_SUPERVISOR_ALLOWED_TOOLS = ["systemctl", "kubectl"]
    tenant = tenant_factory("supervisor-bootstrap", prefix="sb")
    node = node_factory(tenant, "supervisor-bootstrap-node")
    auth = CollectorAuth.objects.create(
        node=node,
        config_consumer_name=f"{node.node_id.hex}-config",
        config_password="secret-config",
    )

    response = client.get(
        reverse("agent_supervisor_bootstrap", args=[node.node_id.hex]),
        {"alloy_service": "custom-alloy"},
        HTTP_AUTHORIZATION=config_auth_header(auth),
    )

    assert response.status_code == 200
    runtime = response.json()["runtime"]
    assert runtime["nova_ui_url"] == "https://ui.example.test"
    assert runtime["desired_path"] == "/agent/supervisor/desired"
    assert runtime["node_id"] == node.node_id.hex
    assert runtime["username"] == auth.config_consumer_name
    assert runtime["password"] == "secret-config"
    assert runtime["alloy_service"] == "custom-alloy"
    assert runtime["supervisor_service"] == "nova-supervisor"
    assert runtime["allow_actions"] is True
    assert runtime["allow_self_update"] is True
    assert runtime["allow_alloy_update"] is True
    assert runtime["allowed_tools"] == ["systemctl", "kubectl"]
    assert "restart_command" not in runtime
    assert runtime["poll_timeout"] == "55s"


def test_supervisor_bootstrap_defaults_allowed_tools_to_deny(client, settings, tenant_factory, node_factory):
    settings.UI_URL = "https://ui.example.test"
    tenant = tenant_factory("supervisor-bootstrap-deny", prefix="sbd")
    node = node_factory(tenant, "supervisor-bootstrap-deny-node")
    auth = CollectorAuth.objects.create(
        node=node,
        config_consumer_name=f"{node.node_id.hex}-config",
        config_password="secret-config",
    )

    response = client.get(
        reverse("agent_supervisor_bootstrap", args=[node.node_id.hex]),
        HTTP_AUTHORIZATION=config_auth_header(auth),
    )

    assert response.status_code == 200
    assert response.json()["runtime"]["allowed_tools"] == []


def test_supervisor_desired_returns_noop_state_and_marks_node_seen(client, settings, tenant_factory, node_factory):
    settings.NOVA_SUPERVISOR_POLL_INTERVAL = "15s"
    tenant = tenant_factory("supervisor-desired", prefix="sd")
    node = node_factory(tenant, "supervisor-desired-node")
    auth = CollectorAuth.objects.create(
        node=node,
        config_consumer_name=f"{node.node_id.hex}-config",
        config_password="secret-config",
    )

    response = client.get(
        reverse("agent_supervisor_desired", args=[node.node_id.hex]),
        {
            "supervisor_version": "0.1.0",
            "supervisor_arch": "amd64",
            "alloy_package_format": "deb",
            "version": "v1.10.2",
        },
        HTTP_AUTHORIZATION=config_auth_header(auth),
    )

    assert response.status_code == 200
    assert response["Cache-Control"] == "no-store"
    assert response["X-Content-Type-Options"] == "nosniff"
    payload = response.json()
    assert payload["version"]
    assert response["ETag"] == f'"{payload["version"]}"'
    assert payload["poll_after"] == "15s"
    assert payload["actions"] == []
    assert payload["alloy"] == {}
    assert payload["supervisor"] == {}
    assert payload["features"] == {
        "containerMonitor": False,
        "dockerLogs": False,
    }
    assert payload["metadata"] == {
        "node_id": node.node_id.hex,
        "enabled": "true",
    }
    node.refresh_from_db()
    assert node.last_seen_at is not None
    assert node.last_config_poll_at is not None
    assert node.alloy_version == "v1.10.2"
    assert node.supervisor_version == "0.1.0"
    assert node.supervisor_arch == "amd64"
    assert node.alloy_package_format == "deb"


def test_supervisor_desired_returns_docker_feature_flags(client, tenant_factory, node_factory):
    tenant = tenant_factory("supervisor-desired-features", prefix="sdf")
    node = node_factory(
        tenant,
        "supervisor-desired-features-node",
        feature_kwargs={"containerMonitor": True, "dockerLogs": True},
    )
    auth = CollectorAuth.objects.create(
        node=node,
        config_consumer_name=f"{node.node_id.hex}-config",
        config_password="secret-config",
    )

    response = client.get(
        reverse("agent_supervisor_desired", args=[node.node_id.hex]),
        HTTP_AUTHORIZATION=config_auth_header(auth),
    )

    assert response.status_code == 200
    assert response.json()["features"] == {
        "containerMonitor": True,
        "dockerLogs": True,
    }


def test_supervisor_desired_returns_update_payloads_when_configured(client, settings, tenant_factory, node_factory):
    sha = "a" * 64
    settings.NOVA_SUPERVISOR_VERSION = "0.3.0"
    settings.NOVA_SUPERVISOR_BINARY_SHA256 = sha
    settings.NOVA_SUPERVISOR_BINARY_URL = "https://assets.example.test/supervisor/{version}/{arch}"
    settings.NOVA_SUPERVISOR_ALLOY_VERSION = "1.11.0"
    settings.NOVA_SUPERVISOR_ALLOY_SHA256 = sha
    settings.NOVA_SUPERVISOR_ALLOY_PACKAGE_URL = "https://assets.example.test/alloy.deb"
    tenant = tenant_factory("supervisor-desired-updates", prefix="sdu")
    node = node_factory(tenant, "supervisor-desired-updates-node")
    auth = CollectorAuth.objects.create(
        node=node,
        config_consumer_name=f"{node.node_id.hex}-config",
        config_password="secret-config",
    )

    response = client.get(
        reverse("agent_supervisor_desired", args=[node.node_id.hex]),
        {
            "supervisor_version": "0.2.0",
            "supervisor_arch": "amd64",
            "version": "v1.10.2",
        },
        HTTP_AUTHORIZATION=config_auth_header(auth),
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["supervisor"] == {
        "version": "0.3.0",
        "manual_update": True,
        "asset_url": "https://assets.example.test/supervisor/0.3.0/amd64",
        "sha256": sha,
        "restart": True,
    }
    assert payload["alloy"] == {
        "version": "v1.11.0",
        "update": True,
        "restart": True,
        "sha256": sha,
        "package_url": "https://assets.example.test/alloy.deb",
    }


def test_supervisor_desired_returns_manual_update_payloads_from_stored_versions(
        client,
        tenant_factory,
        node_factory,
):
    sha = "b" * 64
    supervisor_target = SupervisorVersion.objects.create(version="0.4.0")
    SupervisorVersionArtifact.objects.create(
        version=supervisor_target,
        arch="amd64",
        sha256=sha,
        asset_url="https://assets.example.test/nova-supervisor-0.4.0-linux-amd64",
    )
    alloy_target = AlloyVersion.objects.create(version="v1.12.0")
    AlloyVersionArtifact.objects.create(
        version=alloy_target,
        os="linux",
        arch="amd64",
        package_format="deb",
        sha256=sha,
        package_url="https://assets.example.test/alloy-1.12.0-1.amd64.deb",
    )
    tenant = tenant_factory("supervisor-desired-stored-updates", prefix="sdsu")
    node = node_factory(
        tenant,
        "supervisor-desired-stored-updates-node",
        target_supervisor_version=supervisor_target,
        target_alloy_version=alloy_target,
    )
    auth = CollectorAuth.objects.create(
        node=node,
        config_consumer_name=f"{node.node_id.hex}-config",
        config_password="secret-config",
    )

    response = client.get(
        reverse("agent_supervisor_desired", args=[node.node_id.hex]),
        {
            "supervisor_version": "0.3.0",
            "supervisor_arch": "amd64",
            "alloy_package_format": "deb",
            "version": "v1.11.0",
        },
        HTTP_AUTHORIZATION=config_auth_header(auth),
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["supervisor"] == {
        "version": "0.4.0",
        "manual_update": True,
        "asset_url": "https://assets.example.test/nova-supervisor-0.4.0-linux-amd64",
        "sha256": sha,
        "restart": True,
    }
    assert payload["alloy"] == {
        "version": "v1.12.0",
        "update": True,
        "restart": True,
        "sha256": sha,
        "package_url": "https://assets.example.test/alloy-1.12.0-1.amd64.deb",
    }


def test_supervisor_desired_clears_completed_manual_update_targets(client, tenant_factory, node_factory):
    sha = "d" * 64
    supervisor_target = SupervisorVersion.objects.create(version="0.4.0")
    SupervisorVersionArtifact.objects.create(version=supervisor_target, arch="amd64", sha256=sha)
    alloy_target = AlloyVersion.objects.create(version="v1.12.0")
    AlloyVersionArtifact.objects.create(
        version=alloy_target,
        arch="amd64",
        package_format="deb",
        sha256=sha,
    )
    tenant = tenant_factory("supervisor-desired-completed-updates", prefix="sdcu")
    node = node_factory(
        tenant,
        "supervisor-desired-completed-updates-node",
        target_supervisor_version=supervisor_target,
        target_alloy_version=alloy_target,
    )
    auth = CollectorAuth.objects.create(
        node=node,
        config_consumer_name=f"{node.node_id.hex}-config",
        config_password="secret-config",
    )

    response = client.get(
        reverse("agent_supervisor_desired", args=[node.node_id.hex]),
        {
            "supervisor_version": "0.4.0",
            "supervisor_arch": "amd64",
            "alloy_package_format": "deb",
            "version": "v1.12.0",
        },
        HTTP_AUTHORIZATION=config_auth_header(auth),
    )

    assert response.status_code == 200
    assert response.json()["supervisor"] == {}
    assert response.json()["alloy"] == {}
    node.refresh_from_db()
    assert node.target_supervisor_version is None
    assert node.target_alloy_version is None


def test_supervisor_desired_uses_current_version_and_etag_for_no_change(client, tenant_factory, node_factory):
    tenant = tenant_factory("supervisor-desired-cache", prefix="sdc")
    node = node_factory(tenant, "supervisor-desired-cache-node")
    auth = CollectorAuth.objects.create(
        node=node,
        config_consumer_name=f"{node.node_id.hex}-config",
        config_password="secret-config",
    )

    initial = client.get(
        reverse("agent_supervisor_desired", args=[node.node_id.hex]),
        HTTP_AUTHORIZATION=config_auth_header(auth),
    )
    version = initial.json()["version"]

    current_version = client.get(
        reverse("agent_supervisor_desired", args=[node.node_id.hex]),
        {"current_version": version},
        HTTP_AUTHORIZATION=config_auth_header(auth),
    )
    if_none_match = client.get(
        reverse("agent_supervisor_desired", args=[node.node_id.hex]),
        HTTP_AUTHORIZATION=config_auth_header(auth),
        HTTP_IF_NONE_MATCH=f'"{version}"',
    )

    assert current_version.status_code == 204
    assert current_version["ETag"] == f'"{version}"'
    assert if_none_match.status_code == 304
    assert if_none_match["ETag"] == f'"{version}"'


def test_supervisor_desired_requires_config_auth(client, tenant_factory, node_factory):
    tenant = tenant_factory("supervisor-desired-auth", prefix="sda")
    node = node_factory(tenant, "supervisor-desired-auth-node")
    CollectorAuth.objects.create(
        node=node,
        config_consumer_name=f"{node.node_id.hex}-config",
        config_password="secret-config",
    )

    response = client.get(reverse("agent_supervisor_desired", args=[node.node_id.hex]))

    assert response.status_code == 401


def test_supervisor_bootstrap_rejects_unsafe_public_http_ui_url(client, settings, tenant_factory, node_factory):
    settings.CONFIGURATION_URL = "https://config.example.test"
    settings.UI_URL = "http://public-host.example/novaui"
    settings.NOVA_SUPERVISOR_BINARY_URL = ""
    tenant = tenant_factory("supervisor-bootstrap-unsafe", prefix="sbu")
    node = node_factory(tenant, "supervisor-bootstrap-unsafe-node")
    auth = CollectorAuth.objects.create(
        node=node,
        config_consumer_name=f"{node.node_id.hex}-config",
        config_password="secret-config",
    )

    response = client.get(
        reverse("agent_supervisor_bootstrap", args=[node.node_id.hex]),
        HTTP_AUTHORIZATION=config_auth_header(auth),
    )

    assert response.status_code == 503
    assert response.json()["error"] == "unsafe_agent_url"
    assert "detail" not in response.json()  # Do not expose server configuration.


def test_installer_allows_localhost_http_for_development(client, settings, tenant_factory, node_factory):
    settings.CONFIGURATION_URL = "http://127.0.0.1:8080"
    settings.UI_URL = "http://localhost:8000"
    settings.NOVA_SUPERVISOR_BINARY_URL = "http://[::1]:8000/agent/supervisor/binary/{version}/{arch}"
    tenant = tenant_factory("installer-localhost", prefix="ilh")
    node = node_factory(tenant, "installer-localhost-node")
    token = node.generate_token()

    response = client.get(reverse("installer", args=[token.token]))

    assert response.status_code == 200
    script = response.content.decode("utf-8")
    assert 'url = "http://127.0.0.1:8080"' in script
    assert 'VERSION_REPORT_BASE_URL="http://localhost:8000"' in script
    assert 'SUPERVISOR_URL="${NOVA_SUPERVISOR_BINARY_URL:-http://[::1]:8000/agent/supervisor/binary/{version}/{arch}}"' in script


def test_installer_embeds_arch_specific_supervisor_checksums(client, settings, tmp_path, tenant_factory, node_factory):
    settings.NOVA_SUPERVISOR_VERSION = "0.2.0"
    settings.NOVA_SUPERVISOR_BINARY_DIR = str(tmp_path)
    settings.NOVA_SUPERVISOR_BINARY_SHA256 = ""
    binaries = {
        "amd64": b"supervisor-amd64",
        "arm64": b"supervisor-arm64",
    }
    for arch, content in binaries.items():
        binary = tmp_path / f"nova-supervisor-0.2.0-linux-{arch}"
        binary.write_bytes(content)

    tenant = tenant_factory("installer-supervisor-checksums", prefix="isc")
    node = node_factory(tenant, "installer-supervisor-checksums-node")
    token = node.generate_token()

    response = client.get(reverse("installer", args=[token.token]))

    assert response.status_code == 200
    script = response.content.decode("utf-8")
    for arch, content in binaries.items():
        checksum = hashlib.sha256(content).hexdigest()
        assert f'{arch}) SUPERVISOR_SHA256="{checksum}" ;;' in script


def test_supervisor_binary_endpoint_serves_configured_binary(client, settings, tmp_path):
    binary = tmp_path / "nova-supervisor-0.1.0-linux-amd64"
    binary.write_bytes(b"supervisor-binary")
    settings.NOVA_SUPERVISOR_BINARY_DIR = str(tmp_path)

    response = client.get(reverse("agent_supervisor_binary", args=["0.1.0", "amd64"]))

    assert response.status_code == 200
    assert response["Content-Type"] == "application/octet-stream"
    assert response["X-Content-Type-Options"] == "nosniff"
    assert response["Content-Disposition"] == 'attachment; filename="nova-supervisor-0.1.0-linux-amd64"'
    assert b"".join(response.streaming_content) == b"supervisor-binary"


def test_generating_new_token_revokes_prior_active_token(tenant_factory, node_factory):
    tenant = tenant_factory("token-rotation", prefix="tr")
    node = node_factory(tenant, "token-rotation-node")

    first = node.generate_token()
    second = node.generate_token()

    first.refresh_from_db()
    assert first.revoked_at is not None
    assert second.revoked_at is None
    assert NodeToken.objects.filter(node=node, used_at__isnull=True, revoked_at__isnull=True).count() == 1
