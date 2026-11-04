from unittest.mock import Mock

import pytest
from django.db import transaction

from config.gitops.forgejo import ForgejoClient, bootstrap_tenant_gitops, tenant_gitops_repo_names
from config.integration import create_tenant
from config.models import AuditLog, Tenant, TenantBilling


@pytest.fixture
def tenant(db, monkeypatch):
    monkeypatch.setattr("config.models.create_tenant", Mock())
    return Tenant.objects.create(name="Acme Corp", random_prefix="acme", grafana_org_id=42)


class FakeForgejoClient:
    def __init__(self):
        self.repos = []
        self.files = []

    def ensure_repo(self, name, description):
        from config.gitops.forgejo import ForgejoRepository

        self.repos.append((name, description))
        return ForgejoRepository(
            name=name,
            full_name=f"nova/{name}",
            html_url=f"https://forgejo.example/nova/{name}",
            clone_url=f"https://forgejo.example/nova/{name}.git",
        )

    def ensure_file(self, repo_name, file_path, content, message):
        self.files.append((repo_name, file_path, content, message))
        return True


class FakeResponse:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = ""

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self):
        self.headers = {}
        self.auth = None
        self.calls = []

    def request(self, method, url, **kwargs):
        path = url.split("/api/v1/", 1)[1]
        self.calls.append((method, path, kwargs.get("json")))
        if (method, path) == ("GET", "orgs/nova"):
            return FakeResponse(404)
        if (method, path) == ("POST", "orgs"):
            return FakeResponse(201, {"username": "nova"})
        if (method, path) == ("POST", "orgs/nova/repos"):
            return FakeResponse(
                201,
                {
                    "name": "nova",
                    "full_name": "nova/nova",
                    "html_url": "https://forgejo.example/nova/nova",
                    "clone_url": "https://forgejo.example/nova/nova.git",
                },
            )
        raise AssertionError(f"Unexpected Forgejo call: {method} {path}")


class FakeFileSession:
    def __init__(self, existing=None):
        self.headers = {}
        self.auth = None
        self.existing = existing
        self.calls = []

    def request(self, method, url, **kwargs):
        path = url.split("/api/v1/", 1)[1]
        self.calls.append((method, path, kwargs.get("json")))
        if method == "GET":
            if self.existing is None:
                return FakeResponse(404)
            return FakeResponse(200, self.existing)
        if method == "POST":
            return FakeResponse(201, {})
        if method == "PUT":
            return FakeResponse(200, {})
        raise AssertionError(f"Unexpected Forgejo call: {method} {path}")


def test_forgejo_client_prefers_token_auth(settings):
    settings.GITOPS_FORGEJO_URL = "https://forgejo.example"
    settings.GITOPS_FORGEJO_TOKEN = "token-value"
    settings.GITOPS_FORGEJO_USERNAME = "gitea_admin"
    settings.GITOPS_FORGEJO_PASSWORD = "admin-password"

    client = ForgejoClient()

    assert client.session.headers["Authorization"] == "token token-value"
    assert client.session.auth is None


def test_forgejo_client_supports_basic_auth_without_token(settings):
    settings.GITOPS_FORGEJO_URL = "https://forgejo.example"
    settings.GITOPS_FORGEJO_TOKEN = ""
    settings.GITOPS_FORGEJO_USERNAME = "gitea_admin"
    settings.GITOPS_FORGEJO_PASSWORD = "admin-password"

    client = ForgejoClient()

    assert "Authorization" not in client.session.headers
    assert client.session.auth == ("gitea_admin", "admin-password")


def test_forgejo_client_creates_configured_org_before_repo(settings):
    settings.GITOPS_FORGEJO_URL = "https://forgejo.example"
    settings.GITOPS_FORGEJO_TOKEN = ""
    settings.GITOPS_FORGEJO_USERNAME = "gitea_admin"
    settings.GITOPS_FORGEJO_PASSWORD = "admin-password"
    settings.GITOPS_FORGEJO_ORG = "nova"
    fake_session = FakeSession()

    client = ForgejoClient()
    client.session = fake_session

    repo = client.ensure_repo("nova", "Nova-managed desired state")

    assert repo.full_name == "nova/nova"
    assert fake_session.calls == [
        ("GET", "orgs/nova", None),
        ("POST", "orgs", {"username": "nova", "full_name": "nova"}),
        (
            "POST",
            "orgs/nova/repos",
            {
                "name": "nova",
                "description": "Nova-managed desired state",
                "private": True,
                "auto_init": True,
                "default_branch": "main",
            },
        ),
    ]


def test_forgejo_client_uses_post_when_creating_file(settings):
    settings.GITOPS_FORGEJO_URL = "https://forgejo.example"
    settings.GITOPS_FORGEJO_TOKEN = "token-value"
    fake_session = FakeFileSession()
    client = ForgejoClient()
    client.session = fake_session

    client.write_file("nova/nova", "tenants/nova-acme/README.md", "content", "Create file")

    assert fake_session.calls[0][0] == "GET"
    assert fake_session.calls[1][0] == "POST"
    assert "sha" not in fake_session.calls[1][2]


def test_forgejo_client_uses_put_with_sha_when_updating_file(settings):
    settings.GITOPS_FORGEJO_URL = "https://forgejo.example"
    settings.GITOPS_FORGEJO_TOKEN = "token-value"
    fake_session = FakeFileSession(existing={"sha": "abc123", "content": ""})
    client = ForgejoClient()
    client.session = fake_session

    client.write_file("nova/nova", "README.md", "new content", "Update file")

    assert fake_session.calls[0][0] == "GET"
    assert fake_session.calls[1][0] == "PUT"
    assert fake_session.calls[1][2]["sha"] == "abc123"


def test_bootstrap_tenant_gitops_creates_shared_nova_repo_and_tenant_folders(settings, tenant):
    settings.GITOPS_FORGEJO_ENABLED = True
    fake_client = FakeForgejoClient()

    result = bootstrap_tenant_gitops(tenant, client=fake_client)

    nova_name, custom_name = tenant_gitops_repo_names(tenant)
    assert fake_client.repos == [
        (nova_name, "Nova-managed APISIX and Grafana desired state for all tenants"),
        (custom_name, f"Customer-editable Grafana dashboard config for tenant {tenant.pk}"),
    ]
    assert nova_name == "nova"
    assert (
               nova_name,
               "tenants/nova-acme/apisix/.gitkeep",
               "",
               "Bootstrap tenants/nova-acme/apisix/.gitkeep",
           ) in fake_client.files
    assert (
               nova_name,
               "tenants/nova-acme/grafana/.gitkeep",
               "",
               "Bootstrap tenants/nova-acme/grafana/.gitkeep",
           ) in fake_client.files
    assert (custom_name, "dashboards/.gitkeep", "", "Bootstrap dashboards/.gitkeep") in fake_client.files

    tenant.refresh_from_db()
    assert tenant.gitops_nova_repo == f"nova/{nova_name}"
    assert tenant.gitops_nova_repo_url == f"https://forgejo.example/nova/{nova_name}"
    assert tenant.gitops_custom_repo == f"nova/{custom_name}"
    assert tenant.gitops_custom_repo_url == f"https://forgejo.example/nova/{custom_name}"
    assert result.nova.name == nova_name
    assert result.custom.name == custom_name
    assert AuditLog.objects.filter(action="forgejo.tenant_gitops_bootstrapped", tenant=tenant).exists()


def test_bootstrap_tenant_gitops_skips_when_disabled(settings, tenant):
    settings.GITOPS_FORGEJO_ENABLED = False
    fake_client = FakeForgejoClient()

    result = bootstrap_tenant_gitops(tenant, client=fake_client)

    assert result is None
    assert fake_client.repos == []
    assert fake_client.files == []


def test_create_tenant_bootstraps_forgejo_after_database_commit(settings, transactional_db, monkeypatch):
    settings.GITOPS_FORGEJO_ENABLED = True
    TenantBilling.objects.create(name="Default")
    monkeypatch.setattr("config.models.create_tenant", Mock())

    class FakeOrganization:
        def create_organization(self, organization):
            return {"orgId": 123}

    class FakeOrganizations:
        def delete_organization(self, org_id):
            raise AssertionError("delete_organization should not be called")

    fake_grafana = Mock(organization=FakeOrganization(), organizations=FakeOrganizations())
    monkeypatch.setattr("config.integration.grafana_client", lambda: fake_grafana)
    monkeypatch.setattr("config.integration.update_grafana_org_mapping", Mock())
    bootstrap = Mock()
    monkeypatch.setattr("config.gitops.forgejo.bootstrap_tenant_gitops", bootstrap)
    sync_grafana = Mock()
    monkeypatch.setattr("config.integration._sync_tenant_grafana_gitops", sync_grafana)

    tenant = Tenant.objects.create(name="Beta", random_prefix="beta")
    with transaction.atomic():
        create_tenant(tenant.name, tenant)
        bootstrap.assert_not_called()
        sync_grafana.assert_not_called()

    tenant.refresh_from_db()
    assert tenant.grafana_org_id == 123
    bootstrap.assert_called_once()
    assert bootstrap.call_args.args[0].pk == tenant.pk
    sync_grafana.assert_called_once_with(tenant.pk)
