import logging

from django.apps import apps
from django.db import transaction, IntegrityError
from django.conf import settings

from grafana_client import GrafanaApi
import requests

from .exceptions import GrafanaClientException
from .grafana_urls import grafana_internal_url
from .oidc_bootstrap import get_grafana_oauth_client_credentials

logger = logging.getLogger(__name__)

GRAFANA_TENANT_ROLE_MAPPINGS = (
    ("admin", "Editor"),
    ("editor", "Editor"),
    ("viewer", "Viewer"),
)


def grafana_auth():
    return settings.GRAFANA_USER, settings.GRAFANA_PASSWORD


def grafana_base_url():
    return grafana_internal_url()


def grafana_client():
    return GrafanaApi(
        auth=grafana_auth(),
        host=settings.GRAFANA_HOST,
        protocol=settings.GRAFANA_PROTOCOL,
    )


def grafana_request_timeout():
    return getattr(settings, "GRAFANA_REQUEST_TIMEOUT", 10)


def grafana_org_mappings_for_tenant(tenant) -> list[str]:
    return [
        f"nova-{tenant.random_prefix}-{nova_role}:{tenant.grafana_org_id}:{grafana_role}"
        for nova_role, grafana_role in GRAFANA_TENANT_ROLE_MAPPINGS
    ]


def update_grafana_org_mapping() -> None:
    from config.audit import record_audit

    Tenant = apps.get_model("config", "Tenant")
    issuer_url = settings.NOVA_OIDC_ISSUER_URL.rstrip("/")
    client_id, client_secret = get_grafana_oauth_client_credentials()
    payload = {
        "id": "",
        "settings": {
            "allowAssignGrafanaAdmin": "false",
            "allowSignUp": "true",
            "allowedDomains": "[]",
            "allowedGroups": "[]",
            "allowedOrganizations": "[]",
            "apiUrl": f"{issuer_url}/openid/userinfo",
            "authStyle": "AutoDetect",
            "authUrl": f"{issuer_url}/openid/authorize",
            "autoLogin": "true",
            "clientAuthentication": "",
            "clientId": client_id,
            "clientSecret": client_secret,
            "defineAllowedGroups": "false",
            "defineAllowedTeamsIds": "false",
            "emailAttributeName": "email",
            "emailAttributePath": "email",
            "emptyScopes": "false",
            "enabled": "true",
            "federatedCredentialAudience": "",
            "groupsAttributePath": "",
            "hostedDomain": "",
            "icon": "signin",
            "idTokenAttributeName": "",
            "loginAttributePath": "sub",
            "loginPrompt": "",
            "managedIdentityClientId": "",
            "name": "OAuth",
            "nameAttributePath": "name",
            "roleAttributePath": "'Viewer'",
            "roleAttributeStrict": "true",
            "scopes": "[\"openid\",\"profile\",\"email\",\"groups\",\"roles\"]",
            "signoutRedirectUrl": "",
            "skipOrgRoleSync": "false",
            "teamIds": "[]",
            "teamIdsAttributePath": "",
            "teamsUrl": "",
            "tlsClientCa": "",
            "tlsClientCert": "",
            "tlsClientKey": "",
            "tlsSkipVerifyInsecure": "true" if settings.GRAFANA_OAUTH_TLS_SKIP_VERIFY_INSECURE else "false",
            "tokenUrl": f"{issuer_url}/openid/token",
            "usePkce": "false",
            "useRefreshToken": "true",
            "workloadIdentityTokenFile": "",
            "orgAttributePath": "groups",
        }
    }
    tenants = Tenant.objects.all()
    org_mappings = []
    for tenant in tenants:
        if tenant.grafana_org_id is None:
            continue
        org_mappings.extend(grafana_org_mappings_for_tenant(tenant))
    payload["settings"]["orgMapping"] = org_mappings

    res = requests.put(
        f"{grafana_base_url()}/api/v1/sso-settings/generic_oauth",
        json=payload,
        auth=grafana_auth(),
        timeout=grafana_request_timeout(),
    )
    if res.status_code != 204:
        raise GrafanaClientException("Failed to update Grafana Org Mapping")
    for tenant in tenants:
        if tenant.grafana_org_id is None:
            continue
        record_audit(
            "grafana.org_mapping_updated",
            "Tenant",
            tenant=tenant,
            target_id=tenant.pk,
            metadata={"grafana_org_id": tenant.grafana_org_id},
        )


def create_tenant(tenant_name: str, t, custom_limit: dict = None) -> None:
    from .gitops.forgejo import forgejo_bootstrap_enabled

    TenantLimit = apps.get_model("config", "TenantLimit")
    TenantUsage = apps.get_model("config", "TenantUsage")
    TenantBilling = apps.get_model("config", "TenantBilling")
    # todo allow creating custom limits, for now do it manually
    client = grafana_client()
    grafana_request = client.organization.create_organization(
        organization={"name": tenant_name})
    grafana_org_id = grafana_request.get("orgId")
    if grafana_org_id is None:
        raise GrafanaClientException("Failed to create Grafana Org")

    with transaction.atomic():
        try:
            tb = TenantBilling.objects.filter(name="Default").first()
            tl = TenantLimit(tenant=t, billing=tb)
            tl.save()
            tu = TenantUsage(tenant=t)
            tu.save()
            t.grafana_org_id = grafana_org_id
            t.save()
            update_grafana_org_mapping()
            if forgejo_bootstrap_enabled():
                transaction.on_commit(lambda tenant_id=t.pk: _bootstrap_tenant_gitops_after_commit(tenant_id))
        except IntegrityError:
            logger.exception("Failed to create local tenant records; deleting Grafana org id=%s.", grafana_org_id)
            client.organizations.delete_organization(grafana_org_id)
            raise

        # todo add datasource and dashboards


def _bootstrap_tenant_gitops_after_commit(tenant_id):
    from config.audit import record_audit
    from config.gitops.forgejo import bootstrap_tenant_gitops

    Tenant = apps.get_model("config", "Tenant")

    tenant = Tenant.objects.filter(pk=tenant_id).first()
    if tenant is None:
        return

    try:
        repositories = bootstrap_tenant_gitops(tenant)
        if repositories is not None:
            _sync_tenant_grafana_gitops(tenant.pk)
    except Exception as exc:
        record_audit(
            "forgejo.tenant_gitops_bootstrap_failed",
            "Tenant",
            tenant=tenant,
            target_id=tenant.pk,
            metadata={"error": str(exc)},
        )


def _sync_tenant_grafana_gitops(tenant_id):
    from config.gitops.sync import sync_gitops_resources

    sync_gitops_resources(plugin_names=["grafana"], tenant_id=tenant_id, reason="tenant_created")


def delete_tenant(tenant):
    logger.info("Deleting Grafana org id=%s.", tenant.grafana_org_id)
    grafana_client().organizations.delete_organization(tenant.grafana_org_id)


def logout_user_from_grafana_using_email(email: str):
    try:
        user_id = requests.get(
            f"{grafana_base_url()}/api/users/lookup",
            params={"loginOrEmail": email},
            auth=grafana_auth(),
            timeout=grafana_request_timeout(),
        ).json()["id"]
    # skip logging out if user does not exist in grafana eg. new signup
    except (KeyError, ValueError):
        return
    logger.info("Logging out Grafana user id=%s.", user_id)
    requests.post(
        f"{grafana_base_url()}/api/admin/users/{user_id}/logout/",
        auth=grafana_auth(),
        timeout=grafana_request_timeout(),
    )
