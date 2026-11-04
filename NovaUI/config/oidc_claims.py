from .models import TenantUser


def tenant_claims_for_user(user, tenant=None):
    memberships = TenantUser.objects.select_related("tenant").filter(user=user).order_by("tenant_id", "id")
    if tenant is not None:
        memberships = memberships.filter(tenant=tenant)

    groups = []
    roles = set()
    for membership in memberships:
        groups.append(f"nova-{membership.tenant.random_prefix}-{membership.role}")
        roles.add(membership.role)

    return {
        "groups": groups,
        "roles": sorted(roles),
    }
