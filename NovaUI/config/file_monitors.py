import posixpath

from django.apps import apps
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction

from config.audit import record_audit

DEFAULT_FILE_MONITOR_LIMITS = {
    "starter": 3,
    "team": 5,
    "business": 10,
    "default": 3,
}

SENSITIVE_EXACT_PATHS = {
    "/etc/shadow",
    "/etc/gshadow",
}


def get_file_monitor_limit_for_tenant(tenant):
    TenantLimit = apps.get_model("config", "TenantLimit")

    configured_limits = {
        key.casefold(): value
        for key, value in getattr(settings, "FILE_MONITOR_LIMITS", DEFAULT_FILE_MONITOR_LIMITS).items()
    }
    fallback_limit = int(getattr(settings, "DEFAULT_FILE_MONITOR_LIMIT", configured_limits.get("default", 3)))
    tenant_limit = (
        TenantLimit.objects.filter(tenant=tenant)
        .select_related("billing")
        .order_by("-updated_at")
        .first()
    )
    plan_name = tenant_limit.billing.name if tenant_limit and tenant_limit.billing_id else ""
    return int(configured_limits.get(plan_name.casefold(), fallback_limit))


def get_file_monitor_limit_for_node(node):
    return get_file_monitor_limit_for_tenant(node.tenant)


def normalize_file_monitor_path(file_path):
    value = (file_path or "").strip()
    if not value:
        raise ValidationError("File path is required.")
    if "\x00" in value:
        raise ValidationError("File path contains an invalid character.")
    if not value.startswith("/"):
        raise ValidationError("File path must be an absolute Linux path.")
    normalized = posixpath.normpath(value)
    if normalized == "." or not normalized.startswith("/"):
        raise ValidationError("File path must be an absolute Linux path.")
    return normalized


def _is_sensitive_path(file_path):
    if file_path in SENSITIVE_EXACT_PATHS:
        return True

    parts = set(file_path.split("/"))
    basename = posixpath.basename(file_path)

    if ".ssh" in parts:
        return True
    if file_path.startswith("/etc/ssl/private/"):
        return True
    if file_path.startswith("/etc/letsencrypt/live/") and basename.startswith("privkey"):
        return True
    if basename.endswith((".key", ".pem")) and parts.intersection({"private", "ssl", "tls", "pki"}):
        return True
    return False


def validate_file_monitor_path(file_path):
    normalized = normalize_file_monitor_path(file_path)
    if _is_sensitive_path(normalized):
        raise ValidationError("This path is blocked because it may contain private credentials.")
    return normalized


def validate_file_monitor_specs(*, tenant, specs, limit=None, require_enabled=False):
    limit = limit if limit is not None else get_file_monitor_limit_for_tenant(tenant)
    normalized_specs = []
    seen_paths = set()
    errors = []

    for index, spec in enumerate(specs, start=1):
        monitor_id = spec.get("id")
        label = (spec.get("label") or "").strip()
        raw_path = (spec.get("file_path") or "").strip()
        enabled = bool(spec.get("enabled"))

        if not (monitor_id or label or raw_path or enabled):
            continue

        try:
            file_path = validate_file_monitor_path(raw_path)
        except ValidationError as exc:
            errors.extend(f"File monitor {index}: {message}" for message in exc.messages)
            continue

        if file_path in seen_paths:
            errors.append(f"File monitor {index}: duplicate path {file_path}.")
            continue
        seen_paths.add(file_path)

        normalized_specs.append(
            {
                "id": monitor_id,
                "label": label,
                "file_path": file_path,
                "enabled": enabled,
            }
        )

    if len(normalized_specs) > limit:
        errors.append(f"This plan allows up to {limit} file monitor path(s) per collector.")
    if require_enabled and not any(spec["enabled"] for spec in normalized_specs):
        errors.append("Add at least one enabled file path or disable File/path logs.")

    if errors:
        raise ValidationError(errors)

    return normalized_specs


def active_file_monitor_payload(node):
    monitors = node.file_monitors.filter(enabled=True).order_by("id")
    return [
        {
            "label": monitor.label,
            "path": monitor.file_path,
        }
        for monitor in monitors
    ]


def sync_file_monitors(node, specs, *, actor=None, require_enabled=False):
    FileMonitor = apps.get_model("config", "FileMonitor")
    NodeFeatures = apps.get_model("config", "NodeFeatures")

    normalized_specs = validate_file_monitor_specs(
        tenant=node.tenant,
        specs=specs,
        require_enabled=require_enabled,
    )

    with transaction.atomic():
        existing = {
            monitor.pk: monitor
            for monitor in FileMonitor.objects.select_for_update().filter(node=node)
        }
        submitted_ids = {spec["id"] for spec in normalized_specs if spec.get("id")}
        FileMonitor.objects.filter(node=node).exclude(pk__in=submitted_ids).delete()
        kept_ids = set()

        for spec in normalized_specs:
            monitor_id = spec.get("id")
            monitor = existing.get(monitor_id) if monitor_id else None
            if monitor is None:
                monitor = FileMonitor(node=node)

            monitor.label = spec["label"]
            monitor.file_path = spec["file_path"]
            monitor.enabled = spec["enabled"]
            monitor.full_clean()
            monitor.save()
            kept_ids.add(monitor.pk)

        FileMonitor.objects.filter(node=node).exclude(pk__in=kept_ids).delete()

        record_audit(
            "file_monitor.synced",
            "Node",
            actor=actor,
            tenant=node.tenant,
            target_id=node.pk,
            metadata={
                "node_id": str(node.node_id),
                "count": len(normalized_specs),
                "enabled_count": sum(1 for spec in normalized_specs if spec["enabled"]),
            },
        )

        def push_features():
            try:
                node.features._push_to_redis()
            except NodeFeatures.DoesNotExist:
                return

        transaction.on_commit(push_features)

    return normalized_specs
