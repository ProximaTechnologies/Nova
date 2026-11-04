import hashlib
import json
import re
from dataclasses import dataclass

DNS_LABEL_MAX_LENGTH = 63
GRAFANA_UID_MAX_LENGTH = 40


def canonical_json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def manifest_hash(manifest):
    return hashlib.sha256(canonical_json(manifest).encode("utf-8"), usedforsecurity=False).hexdigest()


def slugify_k8s_name(value, *, max_length=DNS_LABEL_MAX_LENGTH):
    slug = re.sub(r"[^a-z0-9-]+", "-", str(value).lower())
    slug = re.sub(r"-+", "-", slug).strip("-")
    if not slug:
        slug = "resource"
    if len(slug) <= max_length:
        return slug

    digest = hashlib.sha256(slug.encode("utf-8"), usedforsecurity=False).hexdigest()[:8]
    return f"{slug[:max_length - 9].rstrip('-')}-{digest}"


def stable_grafana_uid(*parts):
    raw = "-".join(str(part) for part in parts if part not in (None, ""))
    uid = re.sub(r"[^A-Za-z0-9_-]+", "-", raw).strip("-_")
    if not uid:
        uid = "nova"
    if len(uid) <= GRAFANA_UID_MAX_LENGTH:
        return uid

    digest = hashlib.sha256(uid.encode("utf-8"), usedforsecurity=False).hexdigest()[:8]
    return f"{uid[:GRAFANA_UID_MAX_LENGTH - 9].rstrip('-_')}-{digest}"


def tenant_observability_id(tenant):
    return slugify_k8s_name(f"{tenant.random_prefix}-{tenant.name}", max_length=63)


@dataclass(frozen=True)
class DesiredResource:
    plugin: str
    logical_key: str
    manifest: dict
    tenant: object = None
    relative_path: str = ""

    @property
    def api_version(self):
        return self.manifest.get("apiVersion", "")

    @property
    def kind(self):
        return self.manifest.get("kind", "")

    @property
    def metadata(self):
        return self.manifest.get("metadata", {})

    @property
    def namespace(self):
        return self.metadata.get("namespace", "default")

    @property
    def name(self):
        return self.metadata.get("name", "")

    @property
    def desired_hash(self):
        return manifest_hash(self.manifest)
