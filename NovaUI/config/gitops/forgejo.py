import base64
import binascii
from dataclasses import dataclass
from urllib.parse import quote

import requests
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

from .resources import slugify_k8s_name


class ForgejoBootstrapError(Exception):
    pass


@dataclass(frozen=True)
class ForgejoRepository:
    name: str
    full_name: str
    html_url: str
    clone_url: str
    ssh_url: str = ""


@dataclass(frozen=True)
class TenantGitOpsRepositories:
    nova: ForgejoRepository
    custom: ForgejoRepository


def forgejo_bootstrap_enabled():
    return bool(getattr(settings, "GITOPS_FORGEJO_ENABLED", False))


def tenant_gitops_slug(tenant):
    return slugify_k8s_name(f"{tenant.random_prefix}-{tenant.name}", max_length=80)


def tenant_crd_folder(tenant):
    return slugify_k8s_name(f"nova-{tenant.random_prefix}")


def tenant_gitops_repo_names(tenant):
    slug = tenant_gitops_slug(tenant)
    nova_name = getattr(settings, "GITOPS_FORGEJO_NOVA_REPO_NAME", "nova").strip() or "nova"
    custom_suffix = getattr(settings, "GITOPS_FORGEJO_CUSTOM_REPO_SUFFIX", "custom").strip("-") or "custom"
    return nova_name, f"{slug}-{custom_suffix}"


class ForgejoClient:
    def __init__(self):
        base_url = getattr(settings, "GITOPS_FORGEJO_URL", "").rstrip("/")
        token = getattr(settings, "GITOPS_FORGEJO_TOKEN", "")
        username = getattr(settings, "GITOPS_FORGEJO_USERNAME", "")
        password = getattr(settings, "GITOPS_FORGEJO_PASSWORD", "")
        if not base_url:
            raise ImproperlyConfigured("GITOPS_FORGEJO_URL must be set when Forgejo bootstrap is enabled.")
        if not token and not (username and password):
            raise ImproperlyConfigured(
                "GITOPS_FORGEJO_TOKEN or GITOPS_FORGEJO_USERNAME/GITOPS_FORGEJO_PASSWORD must be set "
                "when Forgejo bootstrap is enabled."
            )

        self.base_url = base_url
        self.org = getattr(settings, "GITOPS_FORGEJO_ORG", "").strip()
        self.default_branch = getattr(settings, "GITOPS_FORGEJO_DEFAULT_BRANCH", "main")
        self.private = bool(getattr(settings, "GITOPS_FORGEJO_REPO_PRIVATE", True))
        self.timeout = getattr(settings, "GITOPS_FORGEJO_TIMEOUT_SECONDS", 10)
        self.verify = bool(getattr(settings, "GITOPS_FORGEJO_SSL_VERIFY", True))
        self.session = requests.Session()
        self.session.headers.update({"Accept": "application/json"})
        if token:
            self.session.headers.update({"Authorization": f"token {token}"})
        else:
            self.session.auth = (username, password)
        self._user_login = ""

    @property
    def owner(self):
        return self.org or self._current_user_login()

    def ensure_repo(self, name, description):
        self.ensure_org()
        payload = {
            "name": name,
            "description": description,
            "private": self.private,
            "auto_init": True,
            "default_branch": self.default_branch,
        }
        endpoint = f"orgs/{quote(self.org, safe='')}/repos" if self.org else "user/repos"
        response = self._request("post", endpoint, json=payload, ok_statuses={201, 409})
        if response.status_code == 409:
            return self.get_repo(name)
        return self._repository_from_payload(response.json())

    def ensure_org(self):
        if not self.org:
            return False

        response = self._request("get", f"orgs/{quote(self.org, safe='')}", ok_statuses={200, 404})
        if response.status_code == 200:
            return False

        payload = {
            "username": self.org,
            "full_name": self.org,
        }
        response = self._request("post", "orgs", json=payload, ok_statuses={201, 422})
        if response.status_code == 422:
            self._request("get", f"orgs/{quote(self.org, safe='')}", ok_statuses={200})
            return False
        return True

    def get_repo(self, name):
        response = self._request("get", self._repo_endpoint(name), ok_statuses={200})
        return self._repository_from_payload(response.json())

    def ensure_file(self, repo_name, file_path, content, message):
        existing = self._get_file(repo_name, file_path)
        if existing is not None:
            return False
        return self.write_file(repo_name, file_path, content, message)

    def write_file(self, repo_name, file_path, content, message):
        existing = self._get_file(repo_name, file_path)
        if existing is not None and _content_matches(existing, content):
            return False

        payload = {
            "branch": self.default_branch,
            "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
            "message": message,
        }
        method = "post"
        ok_statuses = {201}
        if existing is not None:
            method = "put"
            payload["sha"] = existing.get("sha", "")
            ok_statuses = {200}

        self._request(
            method,
            self._file_endpoint(repo_name, file_path),
            json=payload,
            ok_statuses=ok_statuses,
        )
        return True

    def delete_file(self, repo_name, file_path, message):
        existing = self._get_file(repo_name, file_path)
        if existing is None:
            return False

        self._request(
            "delete",
            self._file_endpoint(repo_name, file_path),
            json={
                "branch": self.default_branch,
                "message": message,
                "sha": existing.get("sha", ""),
            },
            ok_statuses={200},
        )
        return True

    def _get_file(self, repo_name, file_path):
        response = self._request("get", self._file_endpoint(repo_name, file_path), ok_statuses={200, 404})
        if response.status_code == 404:
            return None
        return response.json()

    def _repo_endpoint(self, repo_name):
        if "/" in repo_name:
            owner, repo = repo_name.split("/", 1)
        else:
            owner, repo = self.owner, repo_name
        return f"repos/{quote(owner, safe='')}/{quote(repo, safe='')}"

    def _file_endpoint(self, repo_name, file_path):
        encoded_path = quote(file_path.lstrip("/"), safe="/")
        return f"{self._repo_endpoint(repo_name)}/contents/{encoded_path}"

    def _current_user_login(self):
        if self._user_login:
            return self._user_login
        response = self._request("get", "user", ok_statuses={200})
        self._user_login = response.json().get("login", "")
        if not self._user_login:
            raise ForgejoBootstrapError("Forgejo user endpoint did not return a login.")
        return self._user_login

    def _request(self, method, path, *, ok_statuses, **kwargs):
        url = f"{self.base_url}/api/v1/{path.lstrip('/')}"
        response = self.session.request(
            method.upper(),
            url,
            timeout=self.timeout,
            verify=self.verify,
            **kwargs,
        )
        if response.status_code not in ok_statuses:
            raise ForgejoBootstrapError(
                f"Forgejo {method.upper()} {path} failed with HTTP {response.status_code}: {_safe_response_text(response)}"
            )
        return response

    def _repository_from_payload(self, payload):
        return ForgejoRepository(
            name=payload.get("name", ""),
            full_name=payload.get("full_name", ""),
            html_url=payload.get("html_url", ""),
            clone_url=payload.get("clone_url", ""),
            ssh_url=payload.get("ssh_url", ""),
        )


def bootstrap_tenant_gitops(tenant, *, client=None):
    if not forgejo_bootstrap_enabled():
        return None

    from config.audit import record_audit

    client = client or ForgejoClient()
    nova_name, custom_name = tenant_gitops_repo_names(tenant)
    nova_repo = client.ensure_repo(
        nova_name,
        "Nova-managed APISIX and Grafana desired state for all tenants",
    )
    custom_repo = client.ensure_repo(
        custom_name,
        f"Customer-editable Grafana dashboard config for tenant {tenant.pk}",
    )

    for path, content in _nova_seed_files(tenant):
        client.ensure_file(nova_repo.name, path, content, f"Bootstrap {path}")
    for path, content in _custom_seed_files(tenant):
        client.ensure_file(custom_repo.name, path, content, f"Bootstrap {path}")

    tenant.gitops_nova_repo = nova_repo.full_name or nova_repo.name
    tenant.gitops_nova_repo_url = nova_repo.html_url
    tenant.gitops_custom_repo = custom_repo.full_name or custom_repo.name
    tenant.gitops_custom_repo_url = custom_repo.html_url
    tenant.save(
        update_fields=[
            "gitops_nova_repo",
            "gitops_nova_repo_url",
            "gitops_custom_repo",
            "gitops_custom_repo_url",
        ]
    )

    record_audit(
        "forgejo.tenant_gitops_bootstrapped",
        "Tenant",
        tenant=tenant,
        target_id=tenant.pk,
        metadata={
            "nova_repo": tenant.gitops_nova_repo,
            "nova_repo_url": tenant.gitops_nova_repo_url,
            "custom_repo": tenant.gitops_custom_repo,
            "custom_repo_url": tenant.gitops_custom_repo_url,
        },
    )
    return TenantGitOpsRepositories(nova=nova_repo, custom=custom_repo)


def _nova_seed_files(tenant):
    tenant_folder = tenant_crd_folder(tenant)
    return [
        (
            "README.md",
            "\n".join([
                "# Nova-managed GitOps",
                "",
                "This repository is managed by NovaUI.",
                "It stores APISIX and Grafana CRDs under tenants/<tenant>/ folders.",
                "Do not put customer-edited dashboards here; use the tenant custom repository instead.",
                "",
            ]),
        ),
        (f"tenants/{tenant_folder}/README.md", "\n".join([
            f"# {tenant.name}",
            "",
            f"Tenant ID: {tenant.pk}",
            f"Tenant prefix: {tenant.random_prefix}",
            "",
            "NovaUI writes APISIX and Grafana CRDs for this tenant here.",
            "",
        ])),
        (f"tenants/{tenant_folder}/apisix/.gitkeep", ""),
        (f"tenants/{tenant_folder}/grafana/.gitkeep", ""),
    ]


def _custom_seed_files(tenant):
    return [
        (
            "README.md",
            "\n".join([
                "# Customer Grafana Dashboards",
                "",
                f"Tenant ID: {tenant.pk}",
                "",
                "This repository is intended for customer-editable dashboard configuration.",
                "Grafana 12 git-sync should read dashboard files from the dashboards/ directory.",
                "",
            ]),
        ),
        ("dashboards/.gitkeep", ""),
    ]


def _safe_response_text(response):
    text = (getattr(response, "text", "") or "").strip()
    if len(text) > 500:
        return f"{text[:500]}..."
    return text


def _content_matches(payload, expected):
    encoded = (payload.get("content") or "").replace("\n", "")
    if not encoded:
        return False
    try:
        current = base64.b64decode(encoded).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        return False
    return current == expected
