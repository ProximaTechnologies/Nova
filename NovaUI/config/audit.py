from django.apps import apps
from django.db.models import Q


def record_audit(action, target_type, *, actor=None, tenant=None, target_id="", metadata=None):
    AuditLog = apps.get_model("config", "AuditLog")
    return AuditLog.objects.create(
        actor=actor if getattr(actor, "is_authenticated", False) else None,
        tenant=tenant,
        action=action,
        target_type=target_type,
        target_id=str(target_id or ""),
        metadata=metadata or {},
    )


def audit_tenant_secure_id(tenant):
    if not tenant:
        return ""
    return f"{tenant.random_prefix}-{tenant.name}"


def tenant_audit_entries(tenant):
    TenantAuditLogEntry = apps.get_model("config", "TenantAuditLogEntry")
    tenant_filter = Q(tenant=tenant)
    if tenant.pk:
        tenant_filter |= Q(tenant_pk=tenant.pk)
    secure_id = audit_tenant_secure_id(tenant)
    if secure_id:
        tenant_filter |= Q(tenant_secure_id=secure_id)
    return (
        TenantAuditLogEntry.objects.filter(tenant_filter)
        .select_related("actor", "content_type", "tenant")
        .order_by("-timestamp")
    )
