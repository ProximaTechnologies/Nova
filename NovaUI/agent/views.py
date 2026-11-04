import base64
import binascii
import hashlib
import json
import secrets
import uuid
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured, SuspiciousFileOperation, ValidationError
from django.db import transaction
from django.http import FileResponse, Http404, HttpResponse, HttpResponseGone, JsonResponse
from django.utils._os import safe_join
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from config.models import (
    AlloyVersionArtifact,
    Node,
    NodeFeatures,
    NodeToken,
    SupervisorVersionArtifact,
    is_supervisor_semver,
    normalize_alloy_version,
    record_audit,
)
from .models import CollectorAuth
from .provisioning import collector_consumer_name, ensure_collector_auth
from django.shortcuts import get_object_or_404
from django.template.loader import render_to_string
from django.conf import settings
from NovaUI.settings import validate_agent_public_url

SUPPORTED_SUPERVISOR_ARCHES = ("amd64", "arm64", "ppc64le", "s390x")


def _sync_apisix_consumers_after_commit(tenant_id, reason):
    def sync_apisix_consumers():
        from config.gitops.sync import sync_gitops_resources

        sync_gitops_resources(
            plugin_names=["apisix"],
            tenant_id=tenant_id,
            reason=reason,
        )

    transaction.on_commit(sync_apisix_consumers, robust=True)


def _agent_url_configuration_error():
    try:
        validate_agent_public_url("CONFIGURATION_URL", settings.CONFIGURATION_URL)
        validate_agent_public_url("UI_URL", settings.UI_URL)
        validate_agent_public_url(
            "NOVA_SUPERVISOR_BINARY_URL",
            settings.NOVA_SUPERVISOR_BINARY_URL,
            required=False,
        )
    except ImproperlyConfigured as exc:
        return str(exc)
    return None


def _valid_sha256(value):
    return len(value) == 64 and all(char in "0123456789abcdefABCDEF" for char in value)


def _file_sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _supervisor_binary_sha256s():
    version = str(settings.NOVA_SUPERVISOR_VERSION or "").strip()
    if not version:
        return []

    binary_dir = Path(settings.NOVA_SUPERVISOR_BINARY_DIR).resolve()
    template = settings.NOVA_SUPERVISOR_BINARY_NAME_TEMPLATE
    expected_names = {
        arch: template.format(version=version, arch=arch)
        for arch in SUPPORTED_SUPERVISOR_ARCHES
    }
    sha256s = {}

    sums_path = binary_dir / "SHA256SUMS"
    if sums_path.is_file():
        for line in sums_path.read_text(encoding="utf-8").splitlines():
            parts = line.split()
            if len(parts) < 2 or not _valid_sha256(parts[0]):
                continue
            filename = Path(parts[1]).name
            for arch, expected_name in expected_names.items():
                if filename == Path(expected_name).name:
                    path = (binary_dir / expected_name).resolve()
                    if path.is_file() and path.is_relative_to(binary_dir):
                        sha256s[arch] = parts[0].lower()

    for arch, filename in expected_names.items():
        if arch in sha256s:
            continue
        path = (binary_dir / filename).resolve()
        if path.is_file() and path.is_relative_to(binary_dir):
            sha256s[arch] = _file_sha256(path)

    return [
        {"arch": arch, "sha256": sha256s[arch]}
        for arch in SUPPORTED_SUPERVISOR_ARCHES
        if arch in sha256s
    ]


def installer(request, token):
    config_error = _agent_url_configuration_error()
    if config_error:
        return HttpResponse(
            "Unsafe agent URL configuration.",
            status=503,
            content_type="text/plain; charset=utf-8",
        )

    token_obj = get_object_or_404(NodeToken.objects.select_related("node", "node__tenant"), token=token)

    try:
        with transaction.atomic():
            token_obj = NodeToken.objects.select_for_update().select_related("node", "node__tenant").get(
                pk=token_obj.pk)
            token_obj.validate_usable()
            node = token_obj.node
            tenant = node.tenant

            auth, created, repaired_credentials = ensure_collector_auth(node)
            features, _ = NodeFeatures.objects.get_or_create(node=node)
            token_obj.mark_used()
            transaction.on_commit(lambda: features._push_to_redis())
            if created or repaired_credentials:
                _sync_apisix_consumers_after_commit(tenant.pk, "collector_installed")
            record_audit("node_token.used", "NodeToken", tenant=tenant, target_id=token_obj.pk,
                         metadata={
                             "node_id": str(node.node_id),
                             "created_credentials": created,
                             "repaired_credentials": repaired_credentials,
                         })
    except ValidationError as exc:
        record_audit("node_token.rejected", "NodeToken", tenant=token_obj.node.tenant, target_id=token_obj.pk,
                     metadata={"node_id": str(token_obj.node.node_id), "reason": exc.messages})
        return HttpResponseGone("Installer token is no longer valid.")

    context = {
        'node': node,
        'tenant': tenant,
        'username': auth.config_consumer_name or collector_consumer_name(node, "config"),
        'password': auth.config_password,
        'config_url': settings.CONFIGURATION_URL,
        'ui_url': settings.UI_URL,
        'insecure_tls_default': "1" if settings.NOVA_AGENT_INSECURE_TLS else "0",
        'supervisor_binary_url': settings.NOVA_SUPERVISOR_BINARY_URL,
        'supervisor_binary_sha256': settings.NOVA_SUPERVISOR_BINARY_SHA256,
        'supervisor_binary_sha256s': _supervisor_binary_sha256s(),
        'supervisor_version': settings.NOVA_SUPERVISOR_VERSION,
    }

    script = render_to_string("agent/install.sh.j2", context)
    resp = HttpResponse(script, content_type="text/x-sh; charset=utf-8")
    resp["Content-Disposition"] = 'inline; filename="install.sh"'
    resp["X-Content-Type-Options"] = "nosniff"
    resp["Cache-Control"] = "no-store"  # avoid caching tokens in proxies
    return resp


def _basic_auth_credentials(request):
    header = request.headers.get("Authorization", "")
    scheme, _, encoded = header.partition(" ")
    if scheme.lower() != "basic" or not encoded:
        return None, None
    try:
        decoded = base64.b64decode(encoded, validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        return None, None
    username, separator, password = decoded.partition(":")
    if not separator:
        return None, None
    return username, password


def _check_collector_auth(request, auth):
    username, password = _basic_auth_credentials(request)
    expected_username = auth.config_consumer_name or ""
    expected_password = auth.config_password or ""
    return bool(
        username
        and password
        and secrets.compare_digest(username, expected_username)
        and secrets.compare_digest(password, expected_password)
    )

def _check_collector_mimir_auth(request, auth):
    username, password = _basic_auth_credentials(request)
    expected_username = auth.mimir_consumer_name or ""
    expected_password = auth.mimir_password or ""
    return bool(
        username
        and password
        and secrets.compare_digest(username, expected_username)
        and secrets.compare_digest(password, expected_password)
    )

def _check_config_refresh_auth(request):
    expected_token = getattr(settings, "NOVACFG_CONFIG_REFRESH_TOKEN", "")
    if not expected_token:
        return False

    header = request.headers.get("Authorization", "")
    scheme, _, token = header.partition(" ")
    return bool(
        scheme.lower() == "bearer"
        and token
        and secrets.compare_digest(token, expected_token)
    )


def _safe_systemd_service_name(value, default):
    value = str(value or "").strip()
    if not value:
        return default
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789@_.-")
    if all(char in allowed for char in value):
        return value
    return default


def _safe_binary_component(value):
    value = str(value or "").strip()
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-+")
    if not value or value in {".", ".."} or any(char not in allowed for char in value):
        raise Http404("Supervisor binary not found.")
    return value


def _supervisor_binary_sha256_for_arch(arch):
    configured = str(settings.NOVA_SUPERVISOR_BINARY_SHA256 or "").strip().lower()
    if configured:
        return configured if _valid_sha256(configured) else ""
    for item in _supervisor_binary_sha256s():
        if item["arch"] == arch:
            return item["sha256"]
    return ""


def _supervisor_binary_url(version, arch):
    template = str(settings.NOVA_SUPERVISOR_BINARY_URL or "").strip()
    if not template:
        template = f"{settings.UI_URL.rstrip('/')}/agent/supervisor/binary/{{version}}/{{arch}}"
    return template.replace("{version}", version).replace("{arch}", arch)


def _requested_alloy_version(request):
    return normalize_alloy_version(request.GET.get("version") or request.GET.get("alloy_version") or "")


def _supervisor_update_payload(version, arch, sha256, asset_url=""):
    return {
        "version": version,
        "manual_update": True,
        "asset_url": asset_url or _supervisor_binary_url(version, arch),
        "sha256": sha256,
        "restart": True,
    }


def _configured_supervisor_update_desired(request):
    if not settings.NOVA_SUPERVISOR_ALLOW_SELF_UPDATE:
        return {}

    target_version = str(settings.NOVA_SUPERVISOR_VERSION or "").strip()
    current_version = str(request.GET.get("supervisor_version", "")).strip()
    arch = str(request.GET.get("supervisor_arch", "")).strip()
    if (
            not is_supervisor_semver(target_version)
            or current_version == target_version
            or arch not in SUPPORTED_SUPERVISOR_ARCHES
    ):
        return {}

    sha256 = _supervisor_binary_sha256_for_arch(arch)
    if not sha256:
        return {}

    return _supervisor_update_payload(target_version, arch, sha256)


def _supervisor_update_desired(node, request):
    if node.target_supervisor_version_id:
        target = node.target_supervisor_version
        current_version = str(request.GET.get("supervisor_version", "")).strip()
        arch = str(request.GET.get("supervisor_arch", "")).strip()
        if not target.enabled or not arch or current_version == target.version:
            return {}
        artifact = SupervisorVersionArtifact.objects.filter(
            version=target,
            enabled=True,
            arch=arch,
        ).first()
        if artifact is None:
            return {}
        return _supervisor_update_payload(target.version, arch, artifact.sha256, artifact.asset_url)

    return _configured_supervisor_update_desired(request)


def _configured_alloy_update_desired(request):
    if not settings.NOVA_SUPERVISOR_ALLOW_ALLOY_UPDATE:
        return {}

    target_version = normalize_alloy_version(getattr(settings, "NOVA_SUPERVISOR_ALLOY_VERSION", ""))
    current_version = _requested_alloy_version(request)
    sha256 = str(getattr(settings, "NOVA_SUPERVISOR_ALLOY_SHA256", "") or "").strip().lower()
    if not target_version or current_version == target_version or not _valid_sha256(sha256):
        return {}

    desired = {
        "version": target_version,
        "update": True,
        "restart": True,
        "sha256": sha256,
    }
    package_url = str(getattr(settings, "NOVA_SUPERVISOR_ALLOY_PACKAGE_URL", "") or "").strip()
    if package_url:
        desired["package_url"] = package_url
    return desired


def _alloy_artifact_for_request(target, request):
    artifacts = AlloyVersionArtifact.objects.filter(version=target, enabled=True, os="linux")
    arch = str(request.GET.get("supervisor_arch", "")).strip()
    package_format = str(request.GET.get("alloy_package_format", "")).strip()
    if arch:
        artifacts = artifacts.filter(arch=arch)
    if package_format:
        artifacts = artifacts.filter(package_format=package_format)
    artifacts = list(artifacts.order_by("id")[:2])
    if len(artifacts) == 1:
        return artifacts[0]
    return None


def _alloy_update_desired(node, request):
    if node.target_alloy_version_id:
        target = node.target_alloy_version
        target_version = normalize_alloy_version(target.version)
        current_version = _requested_alloy_version(request)
        if not target.enabled or current_version == target_version:
            return {}
        artifact = _alloy_artifact_for_request(target, request)
        if artifact is None:
            return {}
        desired = {
            "version": target_version,
            "update": True,
            "restart": True,
            "sha256": artifact.sha256,
        }
        if artifact.package_url:
            desired["package_url"] = artifact.package_url
        return desired

    return _configured_alloy_update_desired(request)


def _supervisor_desired_state(node, request):
    features, _ = NodeFeatures.objects.get_or_create(node=node)
    payload = {
        "poll_after": settings.NOVA_SUPERVISOR_POLL_INTERVAL,
        "actions": [],
        "alloy": _alloy_update_desired(node, request),
        "supervisor": _supervisor_update_desired(node, request),
        "features": {
            "containerMonitor": bool(node.enabled and features.containerMonitor),
            "dockerLogs": bool(node.enabled and features.dockerLogs),
        },
        "metadata": {
            "node_id": node.node_id.hex,
            "enabled": "true" if node.enabled else "false",
        },
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    payload["version"] = hashlib.sha256(encoded).hexdigest()
    return payload


def _supervisor_etag(version):
    return f'"{version}"'


def _request_has_supervisor_version(request, version):
    current_version = str(request.GET.get("current_version", "")).strip().strip('"')
    if current_version and secrets.compare_digest(current_version, version):
        return True

    if_none_match = request.headers.get("If-None-Match", "")
    for raw_tag in if_none_match.split(","):
        tag = raw_tag.strip()
        if tag.startswith("W/"):
            tag = tag[2:].strip()
        tag = tag.strip('"')
        if tag and secrets.compare_digest(tag, version):
            return True
    return False


def _supervisor_empty_response(version, status):
    response = HttpResponse(status=status)
    response["ETag"] = _supervisor_etag(version)
    response["Cache-Control"] = "no-store"
    return response


def _supervisor_binary_candidates(version, arch):
    version = _safe_binary_component(version)
    arch = _safe_binary_component(arch)
    if arch not in SUPPORTED_SUPERVISOR_ARCHES:
        raise Http404("Supervisor binary not found.")

    binary_dir = Path(settings.NOVA_SUPERVISOR_BINARY_DIR).resolve()
    template = settings.NOVA_SUPERVISOR_BINARY_NAME_TEMPLATE
    names = [template.format(version=version, arch=arch)]
    if version == "latest":
        names.extend([
            f"nova-supervisor-linux-{arch}",
            "nova-supervisor",
        ])

    candidates = []
    for name in names:
        try:
            path = Path(safe_join(str(binary_dir), name))
        except SuspiciousFileOperation:
            continue
        candidates.append(path)
    return candidates


@csrf_exempt  # nosemgrep: python.django.security.audit.csrf-exempt.no-csrf-exempt - M2M endpoint uses bearer auth, not cookie auth.
@require_POST
def refresh_config(request):
    if not getattr(settings, "NOVACFG_CONFIG_REFRESH_TOKEN", ""):
        return JsonResponse({"ok": False, "error": "config_refresh_not_configured"}, status=503)
    if not _check_config_refresh_auth(request):
        return JsonResponse({"ok": False}, status=401)

    try:
        payload = json.loads(request.body.decode("utf-8") or "{}")
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JsonResponse({"ok": False, "error": "invalid_json"}, status=400)

    tenant_id = str(payload.get("tenant_id", "")).strip()
    node_id = str(payload.get("node_id", "")).strip()
    if not tenant_id or not node_id:
        return JsonResponse({"ok": False, "error": "missing_tenant_id_or_node_id"}, status=400)

    try:
        node_uuid = uuid.UUID(node_id)
    except ValueError:
        return HttpResponse(status=404)

    node = get_object_or_404(
        Node.objects.select_related("tenant"),
        tenant__name=tenant_id,
        node_id=node_uuid,
    )
    if node.tenant.billing_suspended:
        return JsonResponse({"ok": False}, status=403)

    features, _ = NodeFeatures.objects.get_or_create(node=node)
    features._push_to_redis()
    return JsonResponse({
        "ok": True,
        "tenant_id": node.tenant.name,
        "node_id": node.node_id.hex,
    })


def _authenticated_collector_node(request, node_id):
    try:
        node_uuid = uuid.UUID(str(node_id))
    except ValueError:
        return None, HttpResponse(status=404)

    auth = get_object_or_404(
        CollectorAuth.objects.select_related(
            "node",
            "node__tenant",
            "node__target_supervisor_version",
            "node__target_alloy_version",
        ),
        node__node_id=node_uuid,
    )
    if not _check_collector_auth(request, auth):
        return None, JsonResponse({"ok": False}, status=401)

    node = auth.node
    if node.tenant.billing_suspended:
        return None, JsonResponse({"ok": False}, status=403)

    return node, None

def _mimir_authenticated_collector_node(request, node_id):
    try:
        node_uuid = uuid.UUID(str(node_id))
    except ValueError:
        return None, HttpResponse(status=404)

    auth = get_object_or_404(CollectorAuth.objects.select_related("node", "node__tenant"), node__node_id=node_uuid)
    if not _check_collector_mimir_auth(request, auth):
        return None, JsonResponse({"ok": False}, status=401)

    node = auth.node
    if node.tenant.billing_suspended:
        return None, JsonResponse({"ok": False}, status=403)

    return node, None


@csrf_exempt  # nosemgrep: python.django.security.audit.csrf-exempt.no-csrf-exempt - Collector endpoint uses basic auth, not cookie auth.
@require_POST
def check_in(request, node_id, version):
    node, response = _mimir_authenticated_collector_node(request, node_id)
    if response is not None:
        return response

    node.mark_seen(alloy_version=version)
    return JsonResponse({
        "ok": True,
        "node_id": node.node_id.hex,
        "status": node.status,
    })


@csrf_exempt  # nosemgrep: python.django.security.audit.csrf-exempt.no-csrf-exempt - Collector endpoint uses basic auth, not cookie auth.
@require_POST
def report_version(request, node_id, version):
    node, response = _authenticated_collector_node(request, node_id)
    if response is not None:
        return response

    node.record_alloy_version(version)
    return JsonResponse({
        "ok": True,
        "node_id": node.node_id.hex,
        "version": node.alloy_version,
        "alloy_version": node.alloy_version,
    })


@csrf_exempt  # nosemgrep: python.django.security.audit.csrf-exempt.no-csrf-exempt - Collector endpoint uses basic auth, not cookie auth.
@require_POST
def report_supervisor_version(request, node_id, version):
    node, response = _authenticated_collector_node(request, node_id)
    if response is not None:
        return response

    node.record_supervisor_version(
        version,
        supervisor_arch=(
            request.GET.get("supervisor_arch")
            or request.GET.get("arch")
            or request.POST.get("supervisor_arch")
            or request.POST.get("arch", "")
        ),
    )
    return JsonResponse({
        "ok": True,
        "node_id": node.node_id.hex,
        "version": node.supervisor_version,
        "supervisor_version": node.supervisor_version,
        "supervisor_arch": node.supervisor_arch,
    })


@require_GET
def supervisor_bootstrap(request, node_id):
    config_error = _agent_url_configuration_error()
    if config_error:
        return JsonResponse({"ok": False, "error": "unsafe_agent_url"}, status=503)

    node, response = _authenticated_collector_node(request, node_id)
    if response is not None:
        return response

    auth = node.collectorauth
    node.record_supervisor_version(
        request.GET.get("supervisor_version", ""),
        supervisor_arch=request.GET.get("supervisor_arch", ""),
    )
    alloy_service = _safe_systemd_service_name(request.GET.get("alloy_service"), "alloy")
    return JsonResponse({
        "runtime": {
            "nova_ui_url": settings.UI_URL.rstrip("/"),
            "desired_path": "/agent/supervisor/desired",
            "node_id": node.node_id.hex,
            "username": auth.config_consumer_name,
            "password": auth.config_password,
            "alloy_service": alloy_service,
            "supervisor_service": "nova-supervisor",
            "allow_actions": settings.NOVA_SUPERVISOR_ALLOW_ACTIONS,
            "allow_self_update": settings.NOVA_SUPERVISOR_ALLOW_SELF_UPDATE,
            "allow_alloy_update": settings.NOVA_SUPERVISOR_ALLOW_ALLOY_UPDATE,
            "allowed_tools": settings.NOVA_SUPERVISOR_ALLOWED_TOOLS,
            "poll_timeout": settings.NOVA_SUPERVISOR_POLL_TIMEOUT,
            "poll_interval": settings.NOVA_SUPERVISOR_POLL_INTERVAL,
            "http_timeout": settings.NOVA_SUPERVISOR_HTTP_TIMEOUT,
        }
    })


@require_GET
def supervisor_desired(request, node_id):
    node, response = _authenticated_collector_node(request, node_id)
    if response is not None:
        return response

    node.mark_seen(
        alloy_version=_requested_alloy_version(request),
        config_poll=True,
        supervisor_version=request.GET.get("supervisor_version", ""),
        supervisor_arch=request.GET.get("supervisor_arch", ""),
        alloy_package_format=request.GET.get("alloy_package_format", ""),
    )
    desired = _supervisor_desired_state(node, request)
    version = desired["version"]

    if _request_has_supervisor_version(request, version):
        status = 304 if request.headers.get("If-None-Match") else 204
        return _supervisor_empty_response(version, status)

    response = JsonResponse(desired)
    response["ETag"] = _supervisor_etag(version)
    response["Cache-Control"] = "no-store"
    response["X-Content-Type-Options"] = "nosniff"
    return response


@require_GET
def supervisor_binary(request, version, arch):
    for path in _supervisor_binary_candidates(version, arch):
        if path.is_file():
            response = FileResponse(
                path.open("rb"),
                as_attachment=True,
                filename=f"nova-supervisor-{version}-linux-{arch}",
                content_type="application/octet-stream",
            )
            response["X-Content-Type-Options"] = "nosniff"
            response["Cache-Control"] = "public, max-age=300" if version == "latest" else "public, max-age=31536000, immutable"
            return response
    raise Http404("Supervisor binary not found.")
