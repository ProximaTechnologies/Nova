from config.models import Tenant, TenantUser
from typing import Tuple
from functools import wraps
from django.core.exceptions import PermissionDenied
from django.http import Http404


def parse_tenant_id_secure(tenant_id_secure) -> Tuple[str, str]:
    try:
        prefix, tenant_name = str(tenant_id_secure).split("-", 1)
    except ValueError as exc:
        raise Http404("Tenant not found.") from exc

    if not prefix or not tenant_name:
        raise Http404("Tenant not found.")
    return prefix, tenant_name


def get_tenant_from_secure_id(tenant_id_secure) -> Tenant:
    prefix, tenant_name = parse_tenant_id_secure(tenant_id_secure)
    tenant = Tenant.objects.filter(name=tenant_name, random_prefix=prefix).first()
    if tenant is None:
        raise Http404("Tenant not found.")
    return tenant


def get_user_tenant(request, tenant_id_secure) -> Tuple[Tenant, str]:
    tenant = get_tenant_from_secure_id(tenant_id_secure)
    if not TenantUser.objects.filter(user=request.user, tenant=tenant).exists():
        raise PermissionDenied
    return tenant, tenant.name


def tenant_role_required(*allowed_roles):
    """
    Example:
    @tenant_role_required("admin", "editor")
    def view(request, tenant_id_secure, ...):
        ...
    """

    def decorator(view_func):
        @wraps(view_func)
        def _wrapped_view(request, tenant_id_secure, *args, **kwargs):
            if not request.user.is_authenticated:
                raise PermissionDenied
            tenant = get_tenant_from_secure_id(tenant_id_secure)

            try:
                tu = TenantUser.objects.get(user=request.user, tenant=tenant)
            except TenantUser.DoesNotExist:
                raise PermissionDenied

            if tu.role not in allowed_roles:
                raise PermissionDenied

            # Optional: attach to request for later use in the view
            request.tenant = tenant
            request.tenant_user = tu

            return view_func(request, tenant_id_secure=tenant_id_secure, *args, **kwargs)

        return _wrapped_view

    return decorator
