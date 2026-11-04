from django.conf import settings

from agent.models import CollectorAuth
from config.gitops.sealed_secrets import seal_secret_data, secret_data_fingerprint
from config.models import GitOpsResource, TenantBillingStatus

from ..resources import DesiredResource, slugify_k8s_name, tenant_observability_id
from .base import GitOpsPlugin

APISIX_API_VERSION = "apisix.apache.org/v2"
APISIX_PLUGIN_CONFIG_API_VERSION = "apisix.apache.org/v2"
SEALED_SECRET_API_VERSION = "bitnami.com/v1alpha1"
EMPTY_CONSUMER_WHITELIST = ["__nova_no_consumers__"]
OBSERVABILITY_TENANT_LABEL = "observability_tenant_id"
OBSERVABILITY_SERVICES = {"mimir", "loki"}

PLUGIN_CONFIG_NAMES = {
    "config": "GITOPS_APISIX_CONFIG_PLUGIN_CONFIG_NAME",
    "mimir": "GITOPS_APISIX_MIMIR_PLUGIN_CONFIG_NAME",
    "loki": "GITOPS_APISIX_LOKI_PLUGIN_CONFIG_NAME",
}


def _tenant_prefix(tenant):
    return slugify_k8s_name(f"nova-{tenant.random_prefix}")


def _managed_labels(plugin, node, service):
    return {
        "nova.proxima.io/managed-by": "novaui",
        "nova.proxima.io/plugin": plugin,
        "nova.proxima.io/tenant-id": str(node.tenant_id),
        "nova.proxima.io/tenant-prefix": node.tenant.random_prefix,
        "nova.proxima.io/node-id": node.node_id.hex,
        "nova.proxima.io/service": service,
    }


def _consumer_labels(plugin, node, service):
    labels = _managed_labels(plugin, node, service)
    if service in OBSERVABILITY_SERVICES:
        labels[OBSERVABILITY_TENANT_LABEL] = tenant_observability_id(node.tenant)
    return labels


def _resource_path(node, kind, name):
    tenant_prefix = _tenant_prefix(node.tenant)
    return f"tenants/{tenant_prefix}/apisix/{kind.lower()}-{name}.yaml"


def _global_resource_path(kind, name):
    return f"global/apisix/{kind.lower()}-{name}.yaml"


def _plugin_config_name(service):
    setting_name = PLUGIN_CONFIG_NAMES[service]
    return slugify_k8s_name(getattr(settings, setting_name))


def _apisix_consumer_identity(namespace, consumer_name):
    # APISIX Ingress Controller flattens Kubernetes namespace/name identities
    # into APISIX consumer names with underscores for consumer-restriction.
    return "_".join(
        slugify_k8s_name(part).replace("-", "_")
        for part in (namespace, consumer_name)
    )


def _global_labels(plugin, service):
    return {
        "nova.proxima.io/managed-by": "novaui",
        "nova.proxima.io/plugin": plugin,
        "nova.proxima.io/service": service,
    }


class APISIXPlugin(GitOpsPlugin):
    name = "apisix"

    def desired_resources(self, tenant=None):
        all_auths = (
            CollectorAuth.objects.select_related("node", "node__tenant")
            .filter(node__enabled=True)
            .order_by("node__tenant_id", "node__node_id")
        )
        if settings.BILLING_ENABLED:
            all_auths = all_auths.exclude(node__tenant__billing_status=TenantBillingStatus.SUSPENDED)
        auths = all_auths
        if tenant is not None:
            auths = auths.filter(node__tenant=tenant)

        for auth in auths:
            for service, consumer_name, password in self._consumer_credentials(auth):
                secret_name = self._secret_name(consumer_name)
                yield self._sealed_secret(auth.node, service, consumer_name, secret_name, password)
                yield self._consumer(auth.node, service, consumer_name, secret_name)

        yield from self._plugin_configs(all_auths)

    def _consumer_credentials(self, auth):
        candidates = (
            ("config", auth.config_consumer_name, auth.config_password),
            ("mimir", auth.mimir_consumer_name, auth.mimir_password),
            ("loki", auth.loki_consumer_name, auth.loki_password),
        )
        for service, consumer_name, password in candidates:
            if consumer_name and password:
                yield service, consumer_name, password

    def _secret_name(self, consumer_name):
        return slugify_k8s_name(f"{consumer_name}-basic-auth")

    def _consumer(self, node, service, consumer_name, secret_name):
        namespace = settings.GITOPS_APISIX_NAMESPACE
        name = slugify_k8s_name(consumer_name)
        manifest = {
            "apiVersion": APISIX_API_VERSION,
            "kind": "ApisixConsumer",
            "metadata": {
                "name": name,
                "namespace": namespace,
                "labels": _consumer_labels(self.name, node, service),
                "annotations": {
                    "nova.proxima.io/secret-ref": secret_name,
                    "nova.proxima.io/secret-contract": "basic-auth username/password keys",
                },
            },
            "spec": {
                "ingressClassName": settings.GITOPS_APISIX_INGRESS_CLASS,
                "authParameter": {
                    "basicAuth": {
                        "secretRef": {
                            "name": secret_name,
                        },
                    },
                },
            },
        }
        return DesiredResource(
            plugin=self.name,
            logical_key=f"node:{node.pk}:consumer:{service}",
            manifest=manifest,
            tenant=node.tenant,
            relative_path=_resource_path(node, "ApisixConsumer", name),
        )

    def _sealed_secret(self, node, service, consumer_name, secret_name, password):
        namespace = settings.GITOPS_APISIX_NAMESPACE
        labels = _managed_labels(self.name, node, service)
        secret_data = {
            "username": consumer_name,
            "password": password,
        }
        logical_key = f"node:{node.pk}:secret:{service}"
        credential_fingerprint = secret_data_fingerprint({
            "name": secret_name,
            "namespace": namespace,
            "data": secret_data,
        })
        encrypted_data = self._existing_encrypted_data(logical_key, credential_fingerprint) or seal_secret_data(
            name=secret_name,
            namespace=namespace,
            data=secret_data,
        )
        manifest = {
            "apiVersion": SEALED_SECRET_API_VERSION,
            "kind": "SealedSecret",
            "metadata": {
                "name": secret_name,
                "namespace": namespace,
                "labels": labels,
                "annotations": {
                    "sealedsecrets.bitnami.com/managed": "true",
                    "nova.proxima.io/secret-contract": "basic-auth username/password keys",
                    "nova.proxima.io/credential-fingerprint": credential_fingerprint,
                },
            },
            "spec": {
                "encryptedData": encrypted_data,
                "template": {
                    "metadata": {
                        "name": secret_name,
                        "namespace": namespace,
                        "labels": labels,
                    },
                    "type": "Opaque",
                },
            },
        }
        return DesiredResource(
            plugin=self.name,
            logical_key=logical_key,
            manifest=manifest,
            tenant=node.tenant,
            relative_path=_resource_path(node, "SealedSecret", secret_name),
        )

    def _existing_encrypted_data(self, logical_key, credential_fingerprint):
        existing = GitOpsResource.objects.filter(plugin=self.name, logical_key=logical_key).first()
        if existing is None:
            return None
        manifest = existing.desired_manifest or {}
        annotations = manifest.get("metadata", {}).get("annotations", {})
        if annotations.get("nova.proxima.io/credential-fingerprint") != credential_fingerprint:
            return None
        encrypted_data = manifest.get("spec", {}).get("encryptedData")
        return encrypted_data or None

    def _plugin_configs(self, auths):
        whitelists = {service: [] for service in PLUGIN_CONFIG_NAMES}
        namespace = settings.GITOPS_APISIX_NAMESPACE
        for auth in auths:
            for service, consumer_name, password in self._consumer_credentials(auth):
                if password:
                    whitelists[service].append(_apisix_consumer_identity(namespace, consumer_name))

        for service in PLUGIN_CONFIG_NAMES:
            yield self._plugin_config(service, sorted(set(whitelists[service])))

    def _plugin_config(self, service, whitelist):
        namespace = settings.GITOPS_APISIX_NAMESPACE
        name = _plugin_config_name(service)
        plugins = [
            {
                "name": "basic-auth",
                "enable": True,
            },
            {
                "name": "consumer-restriction",
                "enable": True,
                "config": {
                    "whitelist": whitelist or EMPTY_CONSUMER_WHITELIST,
                },
            },
        ]
        if service in OBSERVABILITY_SERVICES:
            plugins.append({
                "name": "attach-consumer-label",
                "enable": True,
                "config": {
                    "headers": {
                        "X-Scope-OrgID": f"${OBSERVABILITY_TENANT_LABEL}",
                    },
                },
            })

        manifest = {
            "apiVersion": APISIX_PLUGIN_CONFIG_API_VERSION,
            "kind": "ApisixPluginConfig",
            "metadata": {
                "name": name,
                "namespace": namespace,
                "labels": _global_labels(self.name, service),
            },
            "spec": {
                "ingressClassName": settings.GITOPS_APISIX_INGRESS_CLASS,
                "plugins": plugins,
            },
        }
        return DesiredResource(
            plugin=self.name,
            logical_key=f"plugin-config:{service}",
            manifest=manifest,
            tenant=None,
            relative_path=_global_resource_path("ApisixPluginConfig", name),
        )
