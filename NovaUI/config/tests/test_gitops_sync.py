import json
from unittest.mock import Mock

import pytest
import yaml

from agent.models import CollectorAuth
from config.gitops.forgejo import ForgejoRepository, TenantGitOpsRepositories
from config.gitops.resources import tenant_observability_id
from config.gitops.sync import sync_gitops_resources
from config.models import GitOpsResource, GitOpsResourceStatus, Node, NodeFeatures, Tenant


@pytest.fixture
def tenant(db, monkeypatch):
    monkeypatch.setattr("config.models.create_tenant", lambda *args, **kwargs: None)
    return Tenant.objects.create(name="Acme Corp", random_prefix="acme", grafana_org_id=42)


@pytest.fixture
def gitops_settings(settings, tmp_path):
    settings.GITOPS_REPO_PATH = str(tmp_path)
    settings.GITOPS_MANIFEST_ROOT = "clusters/dev"
    settings.GITOPS_AUTO_COMMIT = False
    settings.GITOPS_AUTO_PUSH = False
    settings.GITOPS_FORGEJO_ENABLED = False
    settings.GRAFANA_PROTOCOL = "http"
    settings.GRAFANA_HOST = "grafana.monitoring.svc.cluster.local:3000"
    settings.GRAFANA_URL = "http://grafana.monitoring.svc.cluster.local:3000"
    settings.GRAFANA_EXTERNAL_URL = "https://grafana.example"
    return tmp_path


class FakeForgejoClient:
    def __init__(self):
        self.written = []
        self.deleted = []

    def write_file(self, repo_name, file_path, content, message):
        self.written.append((repo_name, file_path, content, message))
        return True

    def delete_file(self, repo_name, file_path, message):
        self.deleted.append((repo_name, file_path, message))
        return True


def test_grafana_sync_stores_desired_state_and_writes_crds(tenant, gitops_settings):
    run = sync_gitops_resources(plugin_names=["grafana"], tenant_id=tenant.pk, reason="test")

    assert run.rendered_count == 11
    assert run.synced_count == run.rendered_count

    grafana = GitOpsResource.objects.get(plugin="grafana", kind="Grafana", tenant=tenant)
    assert grafana.sync_status == GitOpsResourceStatus.SYNCED
    assert grafana.desired_manifest["spec"]["external"]["url"] == (
        "http://grafana.monitoring.svc.cluster.local:3000"
    )
    assert grafana.desired_manifest["spec"]["client"]["headers"]["X-Grafana-Org-Id"] == "42"

    manifest_path = gitops_settings / "clusters/dev" / grafana.manifest_path
    assert manifest_path.exists()
    with manifest_path.open(encoding="utf-8") as manifest_file:
        written_manifest = yaml.safe_load(manifest_file)
    assert written_manifest == grafana.desired_manifest

    datasources = GitOpsResource.objects.filter(plugin="grafana", kind="GrafanaDatasource", tenant=tenant)
    assert datasources.count() == 2
    for datasource in datasources:
        assert "orgId" not in datasource.desired_manifest["spec"]["datasource"]

    folder = GitOpsResource.objects.get(plugin="grafana", kind="GrafanaFolder", tenant=tenant)
    assert folder.desired_manifest["spec"]["title"] == "Nova"

    dashboards = GitOpsResource.objects.filter(plugin="grafana", kind="GrafanaDashboard", tenant=tenant)
    assert dashboards.count() == 6
    dashboard_titles = {
        json.loads(dashboard.desired_manifest["spec"]["json"])["title"]
        for dashboard in dashboards
        if "json" in dashboard.desired_manifest["spec"]
    }
    assert dashboard_titles == {
        "Linux Host Overview",
        "Logs Overview",
        "Alloy Agent Health",
        "Tenant Usage / Quota",
    }
    node_exporter_dashboard = dashboards.get(logical_key=f"tenant:{tenant.pk}:dashboard:node-exporter")
    assert node_exporter_dashboard.desired_manifest["spec"]["grafanaCom"] == {"id": 1860}
    assert node_exporter_dashboard.desired_manifest["spec"]["datasources"] == [
        {"inputName": "DS_PROMETHEUS", "datasourceName": "Mimir"},
        {"inputName": "ds_prometheus", "datasourceName": "Mimir"},
    ]
    container_dashboard = dashboards.get(logical_key=f"tenant:{tenant.pk}:dashboard:container-monitoring")
    assert container_dashboard.desired_manifest["spec"]["grafanaCom"] == {"id": 14282}
    linux_dashboard = dashboards.get(logical_key=f"tenant:{tenant.pk}:dashboard:linux-host-overview")
    assert linux_dashboard.desired_manifest["spec"]["folderRef"] == folder.name
    assert linux_dashboard.desired_manifest["spec"]["datasources"] == [
        {"inputName": "DS_MIMIR", "datasourceName": "Mimir"},
        {"inputName": "DS_LOKI", "datasourceName": "Loki"},
    ]
    linux_json = json.loads(linux_dashboard.desired_manifest["spec"]["json"])
    assert linux_json["uid"]
    linux_panel_titles = {panel["title"] for panel in linux_json["panels"]}
    assert {
        "CPU Usage by Host",
        "CPU by Mode",
        "Memory Breakdown",
        "Disk I/O Throughput",
        "Network Throughput",
        "Filesystem Free Space",
    }.issubset(linux_panel_titles)
    assert {"gauge", "table"}.issubset({panel["type"] for panel in linux_json["panels"]})

    logs_dashboard = dashboards.get(logical_key=f"tenant:{tenant.pk}:dashboard:logs-overview")
    logs_json = json.loads(logs_dashboard.desired_manifest["spec"]["json"])
    assert {
        "Warning/Error Rate by Job",
        "Top Warning/Error Jobs",
        "Recent Warnings and Errors",
    }.issubset({panel["title"] for panel in logs_json["panels"]})

    usage_dashboard = dashboards.get(logical_key=f"tenant:{tenant.pk}:dashboard:tenant-usage-quota")
    usage_json = json.loads(usage_dashboard.desired_manifest["spec"]["json"])
    assert "Top Collectors by Samples/sec" in {panel["title"] for panel in usage_json["panels"]}
    assert any(panel["type"] == "bargauge" for panel in usage_json["panels"])

    alert_group = GitOpsResource.objects.get(plugin="grafana", kind="GrafanaAlertRuleGroup", tenant=tenant)
    assert alert_group.desired_manifest["spec"]["folderUID"] == folder.desired_manifest["spec"]["uid"]
    assert "folderRef" not in alert_group.desired_manifest["spec"]
    assert alert_group.desired_manifest["spec"]["name"] == "Nova V1 Alerts"
    assert {rule["title"] for rule in alert_group.desired_manifest["spec"]["rules"]} == {
        "Host down",
        "Disk over 85%",
        "Disk over 95%",
        "High CPU",
        "High memory",
        "Alloy target down",
    }
    for rule in alert_group.desired_manifest["spec"]["rules"]:
        annotations = rule["annotations"]
        assert "__dashboardUid__" not in annotations
        assert "__panelId__" not in annotations


def test_grafana_sync_renders_imported_dashboards_when_container_monitoring_enabled(tenant, gitops_settings):
    node = Node.objects.create(tenant=tenant, node_name="container-collector")
    NodeFeatures.objects.create(node=node, containerMonitor=True)

    run = sync_gitops_resources(plugin_names=["grafana"], tenant_id=tenant.pk, reason="container-dashboard")

    assert run.rendered_count == 11
    dashboards = GitOpsResource.objects.filter(plugin="grafana", kind="GrafanaDashboard", tenant=tenant)
    assert dashboards.count() == 6
    assert dashboards.filter(logical_key=f"tenant:{tenant.pk}:dashboard:node-exporter").exists()
    assert dashboards.filter(logical_key=f"tenant:{tenant.pk}:dashboard:container-monitoring").exists()


def test_apisix_sync_uses_sealed_secret_refs_without_persisting_passwords(tenant, gitops_settings, monkeypatch):
    sealed_calls = []

    def fake_seal_secret_data(*, name, namespace, data):
        sealed_calls.append({"name": name, "namespace": namespace, "data": data})
        return {key: f"sealed-{name}-{key}" for key in data}

    monkeypatch.setattr("config.gitops.plugins.apisix.seal_secret_data", fake_seal_secret_data)
    node = Node.objects.create(tenant=tenant, node_name="collector-1")
    CollectorAuth.objects.create(
        node=node,
        mimir_consumer_name="collector-1-mimir",
        loki_consumer_name="collector-1-loki",
        config_consumer_name="collector-1-config",
        mimir_password="secret-mimir",
        loki_password="secret-loki",
        config_password="secret-config",
    )

    run = sync_gitops_resources(plugin_names=["apisix"], tenant_id=tenant.pk, reason="test")

    assert run.rendered_count == 9
    consumers = GitOpsResource.objects.filter(plugin="apisix", kind="ApisixConsumer", tenant=tenant)
    sealed_secrets = GitOpsResource.objects.filter(plugin="apisix", kind="SealedSecret", tenant=tenant)
    plugin_configs = GitOpsResource.objects.filter(plugin="apisix", kind="ApisixPluginConfig", tenant__isnull=True)
    assert consumers.count() == 3
    assert sealed_secrets.count() == 3
    assert plugin_configs.count() == 3
    sealed_secret_names = {resource.name for resource in sealed_secrets}

    for resource in GitOpsResource.objects.filter(plugin="apisix", tenant=tenant):
        manifest_json = json.dumps(resource.desired_manifest, sort_keys=True)
        assert "secret-mimir" not in manifest_json
        assert "secret-loki" not in manifest_json
        assert "secret-config" not in manifest_json

    for resource in consumers:
        assert resource.desired_manifest["spec"]["authParameter"]["basicAuth"]["secretRef"]["name"]
        assert (
                resource.desired_manifest["spec"]["authParameter"]["basicAuth"]["secretRef"]["name"]
                in sealed_secret_names
        )

    observability_tenant_id = tenant_observability_id(tenant)
    for service in ("mimir", "loki"):
        consumer = consumers.get(logical_key=f"node:{node.pk}:consumer:{service}")
        assert (
            consumer.desired_manifest["metadata"]["labels"]["observability_tenant_id"]
            == observability_tenant_id
        )
    config_consumer = consumers.get(logical_key=f"node:{node.pk}:consumer:config")
    assert "observability_tenant_id" not in config_consumer.desired_manifest["metadata"]["labels"]

    for resource in sealed_secrets:
        manifest = resource.desired_manifest
        assert manifest["apiVersion"] == "bitnami.com/v1alpha1"
        assert manifest["spec"]["encryptedData"]["username"].startswith("sealed-")
        assert manifest["spec"]["encryptedData"]["password"].startswith("sealed-")
        assert "data" not in manifest["spec"]
        assert "stringData" not in manifest["spec"]

    mimir_plugin_config = plugin_configs.get(name="nova-mimir-auth")
    mimir_plugins = mimir_plugin_config.desired_manifest["spec"]["plugins"]
    restriction = mimir_plugins[1]
    assert restriction["name"] == "consumer-restriction"
    assert restriction["config"]["whitelist"] == ["ingress_apisix_collector_1_mimir"]
    attach_label = mimir_plugins[2]
    assert attach_label == {
        "name": "attach-consumer-label",
        "enable": True,
        "config": {
            "headers": {
                "X-Scope-OrgID": "$observability_tenant_id",
            },
        },
    }

    loki_plugin_config = plugin_configs.get(name="nova-loki-auth")
    loki_plugins = loki_plugin_config.desired_manifest["spec"]["plugins"]
    assert loki_plugins[2]["config"]["headers"]["X-Scope-OrgID"] == "$observability_tenant_id"

    config_plugin_config = plugin_configs.get(name="nova-config-auth")
    assert [plugin["name"] for plugin in config_plugin_config.desired_manifest["spec"]["plugins"]] == [
        "basic-auth",
        "consumer-restriction",
    ]

    assert len(sealed_calls) == 3


def test_gitops_sync_can_render_without_git_repo(tenant, settings):
    settings.GITOPS_FORGEJO_ENABLED = False
    settings.GITOPS_REPO_PATH = ""

    run = sync_gitops_resources(plugin_names=["grafana"], tenant_id=tenant.pk, reason="render-only")

    assert run.rendered_count == 11
    assert run.synced_count == 0
    assert GitOpsResource.objects.filter(plugin="grafana", tenant=tenant, sync_status="rendered").exists()


def test_grafana_sync_writes_to_tenant_forgejo_repo_when_enabled(tenant, settings, monkeypatch):
    settings.GITOPS_FORGEJO_ENABLED = True
    settings.GITOPS_REPO_PATH = ""
    tenant.gitops_nova_repo = "nova/nova"
    tenant.save(update_fields=["gitops_nova_repo"])

    fake_client = FakeForgejoClient()
    monkeypatch.setattr("config.gitops.sync.ForgejoClient", lambda: fake_client)
    bootstrap = Mock(side_effect=AssertionError("repo already exists"))
    monkeypatch.setattr("config.gitops.sync.bootstrap_tenant_gitops", bootstrap)

    run = sync_gitops_resources(plugin_names=["grafana"], tenant_id=tenant.pk, reason="forgejo-test")

    assert run.rendered_count == 11
    assert run.synced_count == 11
    assert bootstrap.call_count == 0
    assert len(fake_client.written) == 11
    assert {repo_name for repo_name, _, _, _ in fake_client.written} == {"nova/nova"}

    imported_dashboard_write = next(
        item for item in fake_client.written
        if item[1] == "tenants/nova-acme/grafana/grafanadashboard-nova-acme-node-exporter.yaml"
    )
    imported_dashboard = yaml.safe_load(imported_dashboard_write[2])
    assert imported_dashboard["kind"] == "GrafanaDashboard"
    assert imported_dashboard["spec"]["grafanaCom"] == {"id": 1860}

    dashboard_write = next(
        item for item in fake_client.written
        if item[1] == "tenants/nova-acme/grafana/grafanadashboard-nova-acme-linux-host-overview.yaml"
    )
    dashboard = yaml.safe_load(dashboard_write[2])
    assert dashboard["kind"] == "GrafanaDashboard"
    assert dashboard["spec"]["folderRef"] == "nova-acme-folder"
    assert json.loads(dashboard["spec"]["json"])["title"] == "Linux Host Overview"

    alert_write = next(
        item for item in fake_client.written
        if item[1] == "tenants/nova-acme/grafana/grafanaalertrulegroup-nova-acme-v1-folder-alerts.yaml"
    )
    alert_group = yaml.safe_load(alert_write[2])
    assert alert_group["kind"] == "GrafanaAlertRuleGroup"
    assert alert_group["spec"]["name"] == "Nova V1 Alerts"
    assert alert_group["spec"]["folderUID"] == "folder-acme"


def test_apisix_sync_writes_consumers_and_sealed_secrets_to_forgejo(tenant, settings, monkeypatch):
    settings.GITOPS_FORGEJO_ENABLED = True
    settings.GITOPS_REPO_PATH = ""
    settings.GITOPS_FORGEJO_ORG = "nova"
    tenant.gitops_nova_repo = "nova/nova"
    tenant.save(update_fields=["gitops_nova_repo"])
    monkeypatch.setattr(
        "config.gitops.plugins.apisix.seal_secret_data",
        lambda *, name, namespace, data: {key: f"sealed-{name}-{key}" for key in data},
    )
    fake_client = FakeForgejoClient()
    monkeypatch.setattr("config.gitops.sync.ForgejoClient", lambda: fake_client)
    monkeypatch.setattr("config.gitops.sync.bootstrap_tenant_gitops", Mock(side_effect=AssertionError()))

    node = Node.objects.create(tenant=tenant, node_name="collector-1")
    CollectorAuth.objects.create(
        node=node,
        mimir_consumer_name="collector-1-mimir",
        loki_consumer_name="collector-1-loki",
        config_consumer_name="collector-1-config",
        mimir_password="secret-mimir",
        loki_password="secret-loki",
        config_password="secret-config",
    )

    run = sync_gitops_resources(plugin_names=["apisix"], tenant_id=tenant.pk, reason="forgejo-apisix")

    assert run.rendered_count == 9
    assert run.synced_count == 9
    assert len(fake_client.written) == 9
    assert {repo_name for repo_name, _, _, _ in fake_client.written} == {"nova/nova"}
    assert any(
        path.startswith("tenants/nova-acme/apisix/apisixconsumer-")
        for _, path, _, _ in fake_client.written
    )
    assert any(
        path.startswith("tenants/nova-acme/apisix/sealedsecret-")
        for _, path, _, _ in fake_client.written
    )
    assert any(
        path == "global/apisix/apisixpluginconfig-nova-mimir-auth.yaml"
        for _, path, _, _ in fake_client.written
    )
    for _, _, content, _ in fake_client.written:
        assert "secret-mimir" not in content
        assert "secret-loki" not in content
        assert "secret-config" not in content


def test_forgejo_sync_bootstraps_tenant_repo_when_missing(tenant, settings, monkeypatch):
    settings.GITOPS_FORGEJO_ENABLED = True
    settings.GITOPS_REPO_PATH = ""

    fake_client = FakeForgejoClient()
    monkeypatch.setattr("config.gitops.sync.ForgejoClient", lambda: fake_client)

    def fake_bootstrap(bootstrapped_tenant, *, client):
        assert bootstrapped_tenant == tenant
        assert client is fake_client
        return TenantGitOpsRepositories(
            nova=ForgejoRepository(
                name="nova",
                full_name="nova/nova",
                html_url="https://forgejo.example/nova/nova",
                clone_url="https://forgejo.example/nova/nova.git",
            ),
            custom=ForgejoRepository(
                name="acme-corp-custom",
                full_name="nova/acme-corp-custom",
                html_url="https://forgejo.example/nova/acme-corp-custom",
                clone_url="https://forgejo.example/nova/acme-corp-custom.git",
            ),
        )

    monkeypatch.setattr("config.gitops.sync.bootstrap_tenant_gitops", fake_bootstrap)

    run = sync_gitops_resources(plugin_names=["grafana"], tenant_id=tenant.pk, reason="bootstrap-forgejo")

    assert run.synced_count == 11
    assert {repo_name for repo_name, _, _, _ in fake_client.written} == {"nova/nova"}
