import json

from django.conf import settings

from config.grafana_urls import grafana_internal_url
from config.models import Tenant

from ..resources import DesiredResource, slugify_k8s_name, stable_grafana_uid, tenant_observability_id
from .base import GitOpsPlugin

GRAFANA_API_VERSION = "grafana.integreatly.org/v1beta1"
GRAFANA_ALERT_RULE_GROUP_API_VERSION = GRAFANA_API_VERSION
GRAFANA_DASHBOARD_FOLDER_TITLE = "Nova"
GRAFANA_NODE_EXPORTER_DASHBOARD_ID = 1860
GRAFANA_CONTAINER_DASHBOARD_ID = 14282
MIMIR_INPUT = "DS_MIMIR"
LOKI_INPUT = "DS_LOKI"
PROMETHEUS_INPUT = "DS_PROMETHEUS"
PROMETHEUS_TEMPLATE_INPUT = "ds_prometheus"


def _managed_labels(plugin, tenant, extra=None):
    labels = {
        "nova.proxima.io/managed-by": "novaui",
        "nova.proxima.io/plugin": plugin,
        "nova.proxima.io/tenant-id": str(tenant.pk),
        "nova.proxima.io/tenant-prefix": tenant.random_prefix,
    }
    if extra:
        labels.update(extra)
    return labels


def _tenant_prefix(tenant):
    return slugify_k8s_name(f"nova-{tenant.random_prefix}")


def _resource_path(tenant, kind, name):
    return f"tenants/{_tenant_prefix(tenant)}/grafana/{kind.lower()}-{name}.yaml"


def _desired(plugin, tenant, logical_suffix, manifest):
    kind = manifest["kind"]
    name = manifest["metadata"]["name"]
    return DesiredResource(
        plugin=plugin,
        logical_key=f"tenant:{tenant.pk}:{logical_suffix}",
        manifest=manifest,
        tenant=tenant,
        relative_path=_resource_path(tenant, kind, name),
    )


class GrafanaOperatorPlugin(GitOpsPlugin):
    name = "grafana"

    def desired_resources(self, tenant=None):
        tenants = Tenant.objects.all().order_by("id")
        if tenant is not None:
            tenants = tenants.filter(pk=tenant.pk)

        for tenant in tenants:
            if tenant.grafana_org_id is None:
                continue
            yield from self._tenant_resources(tenant)

    def _tenant_resources(self, tenant):
        namespace = settings.GITOPS_GRAFANA_NAMESPACE
        tenant_prefix = _tenant_prefix(tenant)
        instance_name = slugify_k8s_name(f"{tenant_prefix}-grafana")
        instance_label_key = settings.GITOPS_GRAFANA_INSTANCE_LABEL_KEY
        instance_label = tenant_prefix
        folder_name = slugify_k8s_name(f"{tenant_prefix}-folder")
        folder_uid = stable_grafana_uid("folder", tenant.random_prefix)
        mimir_uid = stable_grafana_uid("mimir", tenant.random_prefix)
        loki_uid = stable_grafana_uid("loki", tenant.random_prefix)
        org_header = str(tenant.grafana_org_id)
        scope_org_id = tenant_observability_id(tenant)

        grafana = {
            "apiVersion": GRAFANA_API_VERSION,
            "kind": "Grafana",
            "metadata": {
                "name": instance_name,
                "namespace": namespace,
                "labels": _managed_labels(self.name, tenant, {instance_label_key: instance_label}),
            },
            "spec": {
                "external": {
                    "url": grafana_internal_url(),
                    "adminUser": {
                        "name": settings.GITOPS_GRAFANA_ADMIN_SECRET_NAME,
                        "key": settings.GITOPS_GRAFANA_ADMIN_USER_KEY,
                    },
                    "adminPassword": {
                        "name": settings.GITOPS_GRAFANA_ADMIN_SECRET_NAME,
                        "key": settings.GITOPS_GRAFANA_ADMIN_PASSWORD_KEY,
                    },
                },
                "client": {"headers": {"X-Grafana-Org-Id": org_header}},
            },
        }
        yield _desired(self.name, tenant, "grafana", grafana)

        instance_selector = {"matchLabels": {instance_label_key: instance_label}}
        folder = {
            "apiVersion": GRAFANA_API_VERSION,
            "kind": "GrafanaFolder",
            "metadata": {
                "name": folder_name,
                "namespace": namespace,
                "labels": _managed_labels(self.name, tenant),
            },
            "spec": {
                "instanceSelector": instance_selector,
                "title": GRAFANA_DASHBOARD_FOLDER_TITLE,
                "uid": folder_uid,
                "resyncPeriod": settings.GITOPS_GRAFANA_RESYNC_PERIOD,
            },
        }
        yield _desired(self.name, tenant, "folder", folder)

        yield self._datasource(
            tenant,
            namespace,
            instance_selector,
            name=slugify_k8s_name(f"{tenant_prefix}-mimir"),
            datasource_uid=mimir_uid,
            datasource_name="Mimir",
            datasource_type="prometheus",
            url=settings.GITOPS_MIMIR_DATASOURCE_URL,
            scope_org_id=scope_org_id,
        )
        yield self._datasource(
            tenant,
            namespace,
            instance_selector,
            name=slugify_k8s_name(f"{tenant_prefix}-loki"),
            datasource_uid=loki_uid,
            datasource_name="Loki",
            datasource_type="loki",
            url=settings.GITOPS_LOKI_DATASOURCE_URL,
            scope_org_id=scope_org_id,
        )

        yield self._grafana_com_dashboard(
            tenant,
            namespace,
            instance_selector,
            folder_name,
            slug="node-exporter",
            title="Node Exporter Full",
            grafana_com_id=GRAFANA_NODE_EXPORTER_DASHBOARD_ID,
        )
        yield self._grafana_com_dashboard(
            tenant,
            namespace,
            instance_selector,
            folder_name,
            slug="container-monitoring",
            title="Container Monitoring",
            grafana_com_id=GRAFANA_CONTAINER_DASHBOARD_ID,
        )

        for slug, title, dashboard in self._v1_dashboards(tenant):
            yield self._dashboard_json(
                tenant,
                namespace,
                instance_selector,
                folder_name,
                slug=slug,
                title=title,
                dashboard=dashboard,
            )

        yield self._alert_rule_group(tenant, namespace, instance_selector, folder_uid, mimir_uid)

    def _datasource(self, tenant, namespace, instance_selector, *, name, datasource_uid, datasource_name,
                    datasource_type, url, scope_org_id):
        datasource = {
            "apiVersion": GRAFANA_API_VERSION,
            "kind": "GrafanaDatasource",
            "metadata": {
                "name": name,
                "namespace": namespace,
                "labels": _managed_labels(self.name, tenant),
            },
            "spec": {
                "instanceSelector": instance_selector,
                "datasource": {
                    "name": datasource_name,
                    "uid": datasource_uid,
                    "type": datasource_type,
                    "access": "proxy",
                    "url": url,
                    "isDefault": datasource_type == "prometheus",
                    "editable": False,
                    "jsonData": {
                        "httpHeaderName1": "X-Scope-OrgID",
                        "timeInterval": "15s",
                    },
                    "secureJsonData": {
                        "httpHeaderValue1": scope_org_id,
                    },
                },
                "resyncPeriod": settings.GITOPS_GRAFANA_RESYNC_PERIOD,
            },
        }
        return _desired(self.name, tenant, f"datasource:{datasource_name.lower()}", datasource)

    def _grafana_com_dashboard(self, tenant, namespace, instance_selector, folder_name, *, slug, title,
                               grafana_com_id):
        name = slugify_k8s_name(f"{_tenant_prefix(tenant)}-{slug}")
        dashboard = {
            "apiVersion": GRAFANA_API_VERSION,
            "kind": "GrafanaDashboard",
            "metadata": {
                "name": name,
                "namespace": namespace,
                "labels": _managed_labels(self.name, tenant),
                "annotations": {
                    "nova.proxima.io/dashboard-title": title,
                    "nova.proxima.io/grafana-com-id": str(grafana_com_id),
                },
            },
            "spec": {
                "instanceSelector": instance_selector,
                "folderRef": folder_name,
                "uid": stable_grafana_uid(slug, tenant.random_prefix),
                "resyncPeriod": settings.GITOPS_GRAFANA_RESYNC_PERIOD,
                "grafanaCom": {"id": grafana_com_id},
                "datasources": [
                    {"inputName": PROMETHEUS_INPUT, "datasourceName": "Mimir"},
                    {"inputName": PROMETHEUS_TEMPLATE_INPUT, "datasourceName": "Mimir"},
                ],
            },
        }
        return _desired(self.name, tenant, f"dashboard:{slug}", dashboard)

    def _dashboard_json(self, tenant, namespace, instance_selector, folder_name, *, slug, title, dashboard):
        name = slugify_k8s_name(f"{_tenant_prefix(tenant)}-{slug}")
        dashboard = {
            "apiVersion": GRAFANA_API_VERSION,
            "kind": "GrafanaDashboard",
            "metadata": {
                "name": name,
                "namespace": namespace,
                "labels": _managed_labels(self.name, tenant),
                "annotations": {
                    "nova.proxima.io/dashboard-title": title,
                },
            },
            "spec": {
                "instanceSelector": instance_selector,
                "folderRef": folder_name,
                "uid": stable_grafana_uid(slug, tenant.random_prefix),
                "resyncPeriod": settings.GITOPS_GRAFANA_RESYNC_PERIOD,
                "json": json.dumps(dashboard, sort_keys=True, separators=(",", ":")),
                "datasources": [
                    {"inputName": MIMIR_INPUT, "datasourceName": "Mimir"},
                    {"inputName": LOKI_INPUT, "datasourceName": "Loki"},
                ],
            },
        }
        return _desired(self.name, tenant, f"dashboard:{slug}", dashboard)

    def _v1_dashboards(self, tenant):
        return (
            (
                "linux-host-overview",
                "Linux Host Overview",
                self._dashboard(
                    tenant,
                    "linux-host-overview",
                    "Linux Host Overview",
                    self._node_template(),
                    [
                        self._stat(1, "Hosts Observed", 0, 0, 6, 4,
                                   'count(count by (instance) (node_uname_info{instance=~"$node"}))'),
                        self._gauge(2, "Average CPU", 6, 0, 6, 4,
                                   'avg(100 - (avg by (instance) (rate(node_cpu_seconds_total{mode="idle",instance=~"$node"}[5m])) * 100))',
                                   unit="percent", thresholds=self._thresholds(70, 90)),
                        self._gauge(3, "Max Memory", 12, 0, 6, 4,
                                   'max((1 - (node_memory_MemAvailable_bytes{instance=~"$node"} / node_memory_MemTotal_bytes{instance=~"$node"})) * 100)',
                                   unit="percent", thresholds=self._thresholds(75, 90)),
                        self._gauge(4, "Max Disk", 18, 0, 6, 4,
                                   'max((1 - (node_filesystem_avail_bytes{instance=~"$node",fstype!~"tmpfs|overlay|squashfs",mountpoint!~"/run.*|/var/lib/docker.*"} / node_filesystem_size_bytes{instance=~"$node",fstype!~"tmpfs|overlay|squashfs",mountpoint!~"/run.*|/var/lib/docker.*"})) * 100)',
                                   unit="percent", thresholds=self._thresholds(80, 95)),
                        self._timeseries(5, "CPU Usage by Host", 0, 4, 12, 7,
                                         '100 - (avg by (instance) (rate(node_cpu_seconds_total{mode="idle",instance=~"$node"}[5m])) * 100)',
                                         "{{ instance }}", unit="percent"),
                        self._timeseries(6, "CPU by Mode", 12, 4, 12, 7,
                                         'avg by (instance, mode) (rate(node_cpu_seconds_total{mode!="idle",instance=~"$node"}[5m])) * 100',
                                         "{{ instance }} {{ mode }}", unit="percent"),
                        self._timeseries(7, "Memory Usage by Host", 0, 11, 12, 7,
                                         '(1 - (node_memory_MemAvailable_bytes{instance=~"$node"} / node_memory_MemTotal_bytes{instance=~"$node"})) * 100',
                                         "{{ instance }}", unit="percent"),
                        self._timeseries(8, "Memory Breakdown", 12, 11, 12, 7,
                                         'node_memory_Active_bytes{instance=~"$node"} or node_memory_Buffers_bytes{instance=~"$node"} or node_memory_Cached_bytes{instance=~"$node"} or node_memory_MemFree_bytes{instance=~"$node"}',
                                         "{{ instance }} {{ __name__ }}", unit="bytes"),
                        self._timeseries(9, "Disk Usage by Mount", 0, 18, 12, 7,
                                         '(1 - (node_filesystem_avail_bytes{instance=~"$node",fstype!~"tmpfs|overlay|squashfs",mountpoint!~"/run.*|/var/lib/docker.*"} / node_filesystem_size_bytes{instance=~"$node",fstype!~"tmpfs|overlay|squashfs",mountpoint!~"/run.*|/var/lib/docker.*"})) * 100',
                                         "{{ instance }} {{ mountpoint }}", unit="percent"),
                        self._timeseries(10, "Disk I/O Throughput", 12, 18, 12, 7,
                                         'sum by (instance, device) (rate(node_disk_read_bytes_total{instance=~"$node"}[5m]) + rate(node_disk_written_bytes_total{instance=~"$node"}[5m]))',
                                         "{{ instance }} {{ device }}", unit="Bps"),
                        self._timeseries(11, "Network Throughput", 0, 25, 12, 7,
                                         'sum by (instance, device) (rate(node_network_receive_bytes_total{instance=~"$node",device!~"lo|veth.*|docker.*|br.*|cni.*"}[5m]) + rate(node_network_transmit_bytes_total{instance=~"$node",device!~"lo|veth.*|docker.*|br.*|cni.*"}[5m]))',
                                         "{{ instance }} {{ device }}", unit="Bps"),
                        self._timeseries(12, "Load Average per Core", 12, 25, 12, 7,
                                         'node_load1{instance=~"$node"} / count by (instance) (node_cpu_seconds_total{mode="idle",instance=~"$node"})',
                                         "{{ instance }}"),
                        self._table(13, "Filesystem Free Space", 0, 32, 24, 7,
                                    'topk(20, node_filesystem_avail_bytes{instance=~"$node",fstype!~"tmpfs|overlay|squashfs",mountpoint!~"/run.*|/var/lib/docker.*"})',
                                    "{{ instance }} {{ mountpoint }}", unit="bytes"),
                    ],
                ),
            ),
            (
                "logs-overview",
                "Logs Overview",
                self._dashboard(
                    tenant,
                    "logs-overview",
                    "Logs Overview",
                    [],
                    [
                        self._loki_stat(1, "Log Lines", 0, 0, 6, 4,
                                        'sum(count_over_time({job=~".+"}[$__range]))'),
                        self._loki_stat(2, "Warnings and Errors", 6, 0, 6, 4,
                                        'sum(count_over_time({job=~".+"} |~ "(?i)(warn|error|failed|panic|fatal)" [$__range]))'),
                        self._loki_stat(3, "File Log Streams", 12, 0, 6, 4,
                                        'count(count_over_time({filename=~".+"}[$__range]))'),
                        self._loki_stat(4, "Active Log Jobs", 18, 0, 6, 4,
                                        'count(count by (job) (count_over_time({job=~".+"}[5m])))'),
                        self._loki_timeseries(5, "Log Rate by Job", 0, 4, 12, 7,
                                              'sum by (job) (rate({job=~".+"}[$__interval]))',
                                              "{{ job }}"),
                        self._loki_timeseries(6, "Warning/Error Rate by Job", 12, 4, 12, 7,
                                              'sum by (job) (rate({job=~".+"} |~ "(?i)(warn|error|failed|panic|fatal)" [$__interval]))',
                                              "{{ job }}"),
                        self._loki_table(7, "Top Log Jobs", 0, 11, 12, 7,
                                         'topk(10, sum by (job) (count_over_time({job=~".+"}[$__range])))',
                                         "{{ job }}"),
                        self._loki_table(8, "Top Warning/Error Jobs", 12, 11, 12, 7,
                                         'topk(10, sum by (job) (count_over_time({job=~".+"} |~ "(?i)(warn|error|failed|panic|fatal)" [$__range])))',
                                         "{{ job }}"),
                        self._loki_table(9, "Top File Log Volume", 0, 18, 12, 7,
                                         'topk(10, sum by (filename) (count_over_time({filename=~".+"}[$__range])))',
                                         "{{ filename }}"),
                        self._logs(10, "Recent Logs", 12, 18, 12, 7, '{job=~".+"}'),
                        self._logs(11, "Recent Warnings and Errors", 0, 25, 12, 8,
                                   '{job=~".+"} |~ "(?i)(warn|error|failed|panic|fatal)"'),
                        self._logs(12, "File Logs", 12, 25, 12, 8, '{filename=~".+"}'),
                    ],
                ),
            ),
            (
                "alloy-agent-health",
                "Alloy Agent Health",
                self._dashboard(
                    tenant,
                    "alloy-agent-health",
                    "Alloy Agent Health",
                    self._node_template(),
                    [
                        self._stat(1, "Alloy Agents", 0, 0, 6, 4,
                                   'count(alloy_build_info{instance=~"$node"})'),
                        self._stat(2, "Running Components", 6, 0, 6, 4,
                                   'sum(alloy_component_controller_running_components{instance=~"$node"})'),
                        self._stat(3, "Scrape Targets Up", 12, 0, 6, 4,
                                   'sum(up{instance=~"$node"})'),
                        self._stat(4, "Scrape Targets Down", 18, 0, 6, 4,
                                   'sum(up{instance=~"$node"} == 0)', thresholds=self._thresholds(1, 5)),
                        self._timeseries(5, "Samples Scraped", 0, 4, 12, 7,
                                         'sum by (instance) (rate(scrape_samples_scraped{instance=~"$node"}[5m]))',
                                         "{{ instance }}"),
                        self._timeseries(6, "Remote Write Samples", 12, 4, 12, 7,
                                         'sum by (instance) (rate(prometheus_remote_write_samples_total{instance=~"$node"}[5m]))',
                                         "{{ instance }}"),
                        self._timeseries(7, "Component Evaluations", 0, 11, 12, 7,
                                         'sum by (instance) (rate(alloy_component_evaluation_total{instance=~"$node"}[5m]))',
                                         "{{ instance }}"),
                        self._timeseries(8, "Scrape Duration p95", 12, 11, 12, 7,
                                         'histogram_quantile(0.95, sum by (instance, le) (rate(scrape_duration_seconds_bucket{instance=~"$node"}[5m])))',
                                         "{{ instance }}", unit="s"),
                        self._timeseries(9, "Remote Write Failed Samples", 0, 18, 12, 7,
                                         'sum by (instance) (rate(prometheus_remote_storage_failed_samples_total{instance=~"$node"}[5m]))',
                                         "{{ instance }}"),
                        self._timeseries(10, "Remote Write Queue Pending", 12, 18, 12, 7,
                                         'sum by (instance) (prometheus_remote_storage_pending_samples{instance=~"$node"})',
                                         "{{ instance }}"),
                        self._table(11, "Scrape Samples by Job", 0, 25, 12, 7,
                                    'topk(15, sum by (job, instance) (rate(scrape_samples_scraped{instance=~"$node"}[5m])))',
                                    "{{ job }} {{ instance }}"),
                        self._table(12, "Down Targets", 12, 25, 12, 7,
                                    'sum by (job, instance) (up{instance=~"$node"} == 0)',
                                    "{{ job }} {{ instance }}"),
                        self._logs(13, "Alloy Service Logs", 0, 32, 24, 8, '{unit="alloy.service"}'),
                    ],
                ),
            ),
            (
                "tenant-usage-quota",
                "Tenant Usage / Quota",
                self._dashboard(
                    tenant,
                    "tenant-usage-quota",
                    "Tenant Usage / Quota",
                    [],
                    [
                        self._stat(1, "Observed Hosts", 0, 0, 6, 4,
                                   'count(count by (instance) (node_uname_info))'),
                        self._stat(2, "Metric Samples / sec", 6, 0, 6, 4,
                                   'sum(rate(scrape_samples_scraped[5m]))'),
                        self._loki_stat(3, "Log Lines / 5m", 12, 0, 6, 4,
                                        'sum(count_over_time({job=~".+"}[5m]))'),
                        self._loki_stat(4, "Warnings and Errors / 5m", 18, 0, 6, 4,
                                        'sum(count_over_time({job=~".+"} |~ "(?i)(warn|error|failed|panic|fatal)" [5m]))'),
                        self._timeseries(5, "Host Count", 0, 4, 12, 7,
                                         'count(count by (instance) (node_uname_info))',
                                         "hosts"),
                        self._loki_timeseries(6, "Log Ingestion Rate", 12, 4, 12, 7,
                                              'sum by (job) (rate({job=~".+"}[$__interval]))',
                                              "{{ job }}"),
                        self._timeseries(7, "Metric Samples by Collector", 0, 11, 12, 7,
                                         'sum by (instance) (rate(scrape_samples_scraped[5m]))',
                                         "{{ instance }}"),
                        self._timeseries(8, "Disk Pressure", 12, 11, 12, 7,
                                         'max by (instance) ((1 - (node_filesystem_avail_bytes{fstype!~"tmpfs|overlay|squashfs",mountpoint!~"/run.*|/var/lib/docker.*"} / node_filesystem_size_bytes{fstype!~"tmpfs|overlay|squashfs",mountpoint!~"/run.*|/var/lib/docker.*"})) * 100)',
                                         "{{ instance }}", unit="percent"),
                        self._timeseries(9, "CPU Pressure", 0, 18, 12, 7,
                                         '100 - (avg by (instance) (rate(node_cpu_seconds_total{mode="idle"}[5m])) * 100)',
                                         "{{ instance }}", unit="percent"),
                        self._timeseries(10, "Memory Pressure", 12, 18, 12, 7,
                                         '(1 - (node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes)) * 100',
                                         "{{ instance }}", unit="percent"),
                        self._bargauge(11, "Top Collectors by Samples/sec", 0, 25, 12, 7,
                                       'topk(10, sum by (instance) (rate(scrape_samples_scraped[5m])))',
                                       "{{ instance }}"),
                        self._loki_table(12, "Top Log Jobs by Volume", 12, 25, 12, 7,
                                         'topk(10, sum by (job) (count_over_time({job=~".+"}[$__range])))',
                                         "{{ job }}"),
                        self._loki_table(13, "Top Files by Log Volume", 0, 32, 12, 7,
                                         'topk(10, sum by (filename) (count_over_time({filename=~".+"}[$__range])))',
                                         "{{ filename }}"),
                        self._loki_timeseries(14, "Warning/Error Log Rate", 12, 32, 12, 7,
                                              'sum by (job) (rate({job=~".+"} |~ "(?i)(warn|error|failed|panic|fatal)" [$__interval]))',
                                              "{{ job }}"),
                    ],
                ),
            ),
        )

    def _dashboard(self, tenant, slug, title, templating, panels):
        return {
            "__inputs": [
                {"name": MIMIR_INPUT, "label": "Mimir", "type": "datasource", "pluginId": "prometheus",
                 "pluginName": "Prometheus"},
                {"name": LOKI_INPUT, "label": "Loki", "type": "datasource", "pluginId": "loki",
                 "pluginName": "Loki"},
            ],
            "uid": stable_grafana_uid(slug, tenant.random_prefix),
            "title": title,
            "tags": ["nova", "v1", tenant.random_prefix],
            "timezone": "browser",
            "schemaVersion": 39,
            "version": 1,
            "refresh": "30s",
            "time": {"from": "now-6h", "to": "now"},
            "templating": {"list": templating},
            "panels": panels,
        }

    def _node_template(self):
        return [
            {
                "name": "node",
                "label": "Node",
                "type": "query",
                "datasource": {"type": "prometheus", "uid": f"${{{MIMIR_INPUT}}}"},
                "query": "label_values(node_uname_info, instance)",
                "refresh": 1,
                "includeAll": True,
                "multi": True,
                "allValue": ".*",
                "current": {"text": "All", "value": "$__all", "selected": False},
            }
        ]

    def _stat(self, panel_id, title, x, y, w, h, expr, *, unit="short", thresholds=None):
        return self._metric_panel(panel_id, title, "stat", x, y, w, h, expr, "A",
                                  unit=unit, thresholds=thresholds)

    def _gauge(self, panel_id, title, x, y, w, h, expr, *, unit="short", thresholds=None):
        return self._metric_panel(panel_id, title, "gauge", x, y, w, h, expr, "A",
                                  unit=unit, thresholds=thresholds, instant=True)

    def _bargauge(self, panel_id, title, x, y, w, h, expr, legend, *, unit="short", thresholds=None):
        return self._metric_panel(panel_id, title, "bargauge", x, y, w, h, expr, legend,
                                  unit=unit, thresholds=thresholds, instant=True)

    def _table(self, panel_id, title, x, y, w, h, expr, legend, *, unit="short"):
        return self._metric_panel(panel_id, title, "table", x, y, w, h, expr, legend,
                                  unit=unit, instant=True, target_format="table")

    def _timeseries(self, panel_id, title, x, y, w, h, expr, legend, *, unit="short"):
        return self._metric_panel(panel_id, title, "timeseries", x, y, w, h, expr, legend, unit=unit)

    def _metric_panel(self, panel_id, title, panel_type, x, y, w, h, expr, legend, *, unit,
                      thresholds=None, instant=False, target_format=None):
        panel = self._panel(panel_id, title, panel_type, x, y, w, h, unit=unit, thresholds=thresholds)
        target = {
            "datasource": {"type": "prometheus", "uid": f"${{{MIMIR_INPUT}}}"},
            "editorMode": "code",
            "expr": expr,
            "legendFormat": legend,
            "range": not instant,
            "refId": "A",
        }
        if instant:
            target["instant"] = True
        if target_format:
            target["format"] = target_format
        panel["datasource"] = {"type": "prometheus", "uid": f"${{{MIMIR_INPUT}}}"}
        panel["targets"] = [target]
        return panel

    def _loki_stat(self, panel_id, title, x, y, w, h, expr):
        return self._loki_panel(panel_id, title, "stat", x, y, w, h, expr, "A", instant=True)

    def _loki_timeseries(self, panel_id, title, x, y, w, h, expr, legend):
        return self._loki_panel(panel_id, title, "timeseries", x, y, w, h, expr, legend)

    def _loki_table(self, panel_id, title, x, y, w, h, expr, legend, *, unit="short"):
        return self._loki_panel(panel_id, title, "table", x, y, w, h, expr, legend,
                                unit=unit, instant=True)

    def _logs(self, panel_id, title, x, y, w, h, expr):
        return self._loki_panel(panel_id, title, "logs", x, y, w, h, expr, "")

    def _loki_panel(self, panel_id, title, panel_type, x, y, w, h, expr, legend, *,
                    unit="short", instant=False):
        panel = self._panel(panel_id, title, panel_type, x, y, w, h, unit=unit)
        target = {
            "datasource": {"type": "loki", "uid": f"${{{LOKI_INPUT}}}"},
            "expr": expr,
            "legendFormat": legend,
            "refId": "A",
        }
        if panel_type != "logs":
            target["range"] = not instant
        if instant:
            target["instant"] = True
        panel["datasource"] = {"type": "loki", "uid": f"${{{LOKI_INPUT}}}"}
        panel["targets"] = [target]
        return panel

    def _thresholds(self, warning, critical):
        return {
            "mode": "absolute",
            "steps": [
                {"color": "green", "value": None},
                {"color": "orange", "value": warning},
                {"color": "red", "value": critical},
            ],
        }

    def _panel(self, panel_id, title, panel_type, x, y, w, h, *, unit="short", thresholds=None):
        defaults = {"unit": unit}
        if thresholds:
            defaults["thresholds"] = thresholds
        panel = {
            "id": panel_id,
            "title": title,
            "type": panel_type,
            "gridPos": {"x": x, "y": y, "w": w, "h": h},
            "fieldConfig": {"defaults": defaults, "overrides": []},
            "options": {},
        }
        if panel_type == "stat":
            panel["options"] = {
                "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                "orientation": "auto",
                "textMode": "auto",
            }
        elif panel_type == "gauge":
            panel["options"] = {
                "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                "showThresholdLabels": False,
                "showThresholdMarkers": True,
            }
        elif panel_type == "bargauge":
            panel["options"] = {
                "displayMode": "gradient",
                "orientation": "horizontal",
                "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                "showUnfilled": True,
            }
        elif panel_type == "timeseries":
            panel["options"] = {
                "legend": {"calcs": ["lastNotNull"], "displayMode": "table", "placement": "bottom"},
                "tooltip": {"mode": "multi", "sort": "desc"},
            }
        elif panel_type == "table":
            panel["options"] = {"cellHeight": "sm", "showHeader": True}
        elif panel_type == "logs":
            panel["options"] = {"showLabels": False, "showTime": True, "wrapLogMessage": True}
        return panel

    def _alert_rule_group(self, tenant, namespace, instance_selector, folder_uid, mimir_uid):
        name = slugify_k8s_name(f"{_tenant_prefix(tenant)}-v1-folder-alerts")
        rules = [
            self._prometheus_alert_rule(
                tenant,
                mimir_uid,
                "host-down",
                "Host down",
                'up{instance=~".+"}',
                "lt",
                [1],
                "5m",
                "critical",
                "A host scrape target is down.",
            ),
            self._prometheus_alert_rule(
                tenant,
                mimir_uid,
                "disk-85",
                "Disk over 85%",
                '(1 - (node_filesystem_avail_bytes{fstype!~"tmpfs|overlay|squashfs",mountpoint!~"/run.*|/var/lib/docker.*"} / node_filesystem_size_bytes{fstype!~"tmpfs|overlay|squashfs",mountpoint!~"/run.*|/var/lib/docker.*"})) * 100',
                "gt",
                [85],
                "10m",
                "warning",
                "A filesystem is over 85% full.",
            ),
            self._prometheus_alert_rule(
                tenant,
                mimir_uid,
                "disk-95",
                "Disk over 95%",
                '(1 - (node_filesystem_avail_bytes{fstype!~"tmpfs|overlay|squashfs",mountpoint!~"/run.*|/var/lib/docker.*"} / node_filesystem_size_bytes{fstype!~"tmpfs|overlay|squashfs",mountpoint!~"/run.*|/var/lib/docker.*"})) * 100',
                "gt",
                [95],
                "5m",
                "critical",
                "A filesystem is over 95% full.",
            ),
            self._prometheus_alert_rule(
                tenant,
                mimir_uid,
                "high-cpu",
                "High CPU",
                '100 - (avg by (instance) (rate(node_cpu_seconds_total{mode="idle"}[5m])) * 100)',
                "gt",
                [90],
                "15m",
                "warning",
                "Average CPU usage is above 90%.",
            ),
            self._prometheus_alert_rule(
                tenant,
                mimir_uid,
                "high-memory",
                "High memory",
                "(1 - (node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes)) * 100",
                "gt",
                [90],
                "15m",
                "warning",
                "Memory usage is above 90%.",
            ),
            self._prometheus_alert_rule(
                tenant,
                mimir_uid,
                "alloy-target-down",
                "Alloy target down",
                'up{job=~".*alloy.*"}',
                "lt",
                [1],
                "5m",
                "critical",
                "An Alloy scrape target is down or missing.",
            ),
        ]
        manifest = {
            "apiVersion": GRAFANA_ALERT_RULE_GROUP_API_VERSION,
            "kind": "GrafanaAlertRuleGroup",
            "metadata": {
                "name": name,
                "namespace": namespace,
                "labels": _managed_labels(self.name, tenant),
            },
            "spec": {
                "instanceSelector": instance_selector,
                "folderUID": folder_uid,
                "name": "Nova V1 Alerts",
                "interval": "1m",
                "resyncPeriod": settings.GITOPS_GRAFANA_RESYNC_PERIOD,
                "rules": rules,
            },
        }
        return _desired(self.name, tenant, "alert-rule-group:v1-folder-uid", manifest)

    def _prometheus_alert_rule(self, tenant, mimir_uid, slug, title, expr, evaluator, params, duration, severity,
                               summary):
        return {
            "uid": stable_grafana_uid("alert", slug, tenant.random_prefix),
            "title": title,
            "condition": "C",
            "for": duration,
            "noDataState": "NoData",
            "execErrState": "Error",
            "isPaused": False,
            "labels": {
                "severity": severity,
                "tenant": tenant.random_prefix,
                "managed_by": "novaui",
            },
            "annotations": {
                "summary": summary,
            },
            "data": [
                {
                    "refId": "A",
                    "datasourceUid": mimir_uid,
                    "relativeTimeRange": {"from": 600, "to": 0},
                    "model": {
                        "datasource": {"type": "prometheus", "uid": mimir_uid},
                        "editorMode": "code",
                        "expr": expr,
                        "instant": True,
                        "intervalMs": 1000,
                        "legendFormat": "__auto",
                        "maxDataPoints": 43200,
                        "range": False,
                        "refId": "A",
                    },
                },
                {
                    "refId": "B",
                    "datasourceUid": "__expr__",
                    "relativeTimeRange": {"from": 600, "to": 0},
                    "model": self._expression_model("B", "reduce", "A", reducer="last"),
                },
                {
                    "refId": "C",
                    "datasourceUid": "__expr__",
                    "relativeTimeRange": {"from": 600, "to": 0},
                    "model": self._expression_model("C", "threshold", "B", evaluator=evaluator, params=params),
                },
            ],
        }

    def _expression_model(self, ref_id, expression_type, expression, *, reducer=None, evaluator=None, params=None):
        model = {
            "datasource": {"type": "__expr__", "uid": "__expr__"},
            "expression": expression,
            "intervalMs": 1000,
            "maxDataPoints": 43200,
            "refId": ref_id,
            "type": expression_type,
        }
        if reducer:
            model["reducer"] = reducer
        if evaluator:
            model["conditions"] = [
                {
                    "evaluator": {"params": params or [], "type": evaluator},
                    "operator": {"type": "and"},
                    "query": {"params": [ref_id]},
                    "reducer": {"params": [], "type": "last"},
                    "type": "query",
                }
            ]
        return model
