import hashlib

from django.conf import settings
from django.contrib import admin

from .models import (
    AuditLog,
    AlloyVersion,
    AlloyVersionArtifact,
    FileMonitor,
    GitOpsResource,
    GitOpsSyncRun,
    SupervisorVersion,
    SupervisorVersionArtifact,
    Tenant,
    TenantBilling,
    TenantLimit,
    TenantUser,
    Node,
    NodeToken,
    NodeFeatures,
    TenantUsage,
)
from billing.models import Billing


class BillingInline(admin.TabularInline):
    model = Billing
    extra = 0
    fields = ("billing_type", "status", "due_date", "total", "created_at")
    readonly_fields = ("created_at",)


class TenantAdmin(admin.ModelAdmin):
    list_display = ('name', 'billing_status', 'grafana_org_id', 'random_prefix', 'created_at', 'updated_at')
    readonly_fields = ('grafana_org_id', 'random_prefix', 'created_at', 'updated_at')
    search_fields = ('name',)
    list_filter = ('billing_status',)
    def get_inlines(self, request, obj=None):
        return [BillingInline] if settings.BILLING_ENABLED else []
    actions = ("sync_grafana_gitops",)

    @admin.action(description="Sync Grafana dashboards and alerts")
    def sync_grafana_gitops(self, request, queryset):
        from config.gitops.sync import sync_gitops_resources

        synced = 0
        rendered_count = 0
        written_count = 0
        failures = []
        for tenant in queryset:
            try:
                run = sync_gitops_resources(
                    plugin_names=["grafana"],
                    tenant_id=tenant.pk,
                    reason="admin_grafana_sync",
                    actor=request.user,
                )
            except Exception as exc:
                failures.append(f"{tenant.name}: {exc}")
                continue

            synced += 1
            rendered_count += run.rendered_count
            written_count += run.synced_count

        if synced:
            self.message_user(
                request,
                f"Synced Grafana GitOps for {synced} tenant(s): "
                f"{rendered_count} rendered, {written_count} written.",
                level="success",
            )
        if failures:
            self.message_user(
                request,
                "Grafana GitOps sync failed for " + "; ".join(failures),
                level="error",
            )


class TenantBillingAdmin(admin.ModelAdmin):
    list_display = ("name", "price_per_node", "price_per_cluster", "price_per_vcpu")
    search_fields = ('name',)


class TenantLimitAdmin(admin.ModelAdmin):
    list_display = ("tenant_name", "max_nodes", "max_clusters", "max_vcpus", "billing", "created_at", "updated_at")
    search_fields = ('tenant_name',)
    autocomplete_fields = ('billing', 'tenant')

    def tenant_name(self, obj):
        return obj.tenant.name


class TenantUserAdmin(admin.ModelAdmin):
    list_display = ("tenant_name", "user", "role", "created_at", "updated_at")
    search_fields = ('tenant_name', 'user')
    autocomplete_fields = ('tenant', 'user')

    def tenant_name(self, obj):
        return obj.tenant.name


class NodeFeatureInline(admin.TabularInline):
    model = NodeFeatures
    extra = 0
    autocomplete_fields = ('node',)


class FileMonitorInline(admin.TabularInline):
    model = FileMonitor
    extra = 0
    fields = ("label", "file_path", "enabled", "created_at", "updated_at")
    readonly_fields = ("created_at", "updated_at")


class SupervisorVersionArtifactInline(admin.TabularInline):
    model = SupervisorVersionArtifact
    extra = 1
    fields = ("arch", "sha256", "asset_url", "enabled", "created_at", "updated_at")
    readonly_fields = ("created_at", "updated_at")


class AlloyVersionArtifactInline(admin.TabularInline):
    model = AlloyVersionArtifact
    extra = 1
    fields = ("os", "arch", "package_format", "sha256", "package_url", "enabled", "created_at", "updated_at")
    readonly_fields = ("created_at", "updated_at")


class TenantUsageAdmin(admin.ModelAdmin):
    list_display = ("tenant_name", "nodes", "clusters", "vcpus", "created_at")
    search_fields = ("tenant_name",)
    autocomplete_fields = ("tenant",)

    def tenant_name(self, obj):
        return obj.tenant.name


class NodeAdmin(admin.ModelAdmin):
    list_display = ("node_name", "tenant_name", "status_label", "enabled", "alloy_version", "supervisor_version",
                    "last_seen_at", "last_config_poll_at", "created_at", "updated_at")
    list_editable = ("enabled",)
    search_fields = ("node_name", "tenant__name")
    autocomplete_fields = ("tenant", "target_supervisor_version", "target_alloy_version")
    inlines = [NodeFeatureInline, FileMonitorInline]

    def tenant_name(self, obj):
        return obj.tenant.name


class NodeTokenAdmin(admin.ModelAdmin):
    list_display = ("node", "token_fingerprint", "created_at", "expires_at", "used_at", "revoked_at")
    list_filter = ("created_at", "expires_at", "used_at", "revoked_at")
    search_fields = ("node__node_name", "node__node_id")
    readonly_fields = ("token_fingerprint", "created_at", "expires_at", "used_at", "revoked_at")
    exclude = ("token",)

    def token_fingerprint(self, obj):
        if not obj.token:
            return "-"
        return hashlib.sha256(obj.token.encode("utf-8")).hexdigest()[:12]

    token_fingerprint.short_description = "Token fingerprint"


class AuditLogAdmin(admin.ModelAdmin):
    list_display = ("created_at", "action", "tenant", "actor", "target_type", "target_id")
    list_filter = ("action", "target_type", "created_at")
    search_fields = ("action", "target_type", "target_id", "tenant__name", "actor__username")
    readonly_fields = ("actor", "tenant", "action", "target_type", "target_id", "metadata", "created_at")


class SupervisorVersionAdmin(admin.ModelAdmin):
    list_display = ("version", "enabled", "artifact_count", "created_at", "updated_at")
    list_filter = ("enabled",)
    search_fields = ("version",)
    inlines = [SupervisorVersionArtifactInline]

    def artifact_count(self, obj):
        return obj.artifacts.count()


class AlloyVersionAdmin(admin.ModelAdmin):
    list_display = ("version", "enabled", "artifact_count", "created_at", "updated_at")
    list_filter = ("enabled",)
    search_fields = ("version",)
    inlines = [AlloyVersionArtifactInline]

    def artifact_count(self, obj):
        return obj.artifacts.count()


class GitOpsResourceAdmin(admin.ModelAdmin):
    list_display = ("plugin", "kind", "namespace", "name", "tenant", "sync_status", "updated_at")
    list_filter = ("plugin", "kind", "sync_status", "namespace")
    search_fields = ("plugin", "logical_key", "kind", "namespace", "name", "tenant__name")
    readonly_fields = (
        "tenant",
        "plugin",
        "logical_key",
        "api_version",
        "kind",
        "namespace",
        "name",
        "manifest_path",
        "desired_manifest",
        "desired_hash",
        "last_applied_hash",
        "sync_status",
        "last_error",
        "last_rendered_at",
        "last_synced_at",
        "created_at",
        "updated_at",
    )


class GitOpsSyncRunAdmin(admin.ModelAdmin):
    list_display = ("started_at", "reason", "plugin", "tenant", "status", "rendered_count", "synced_count",
                    "pruned_count")
    list_filter = ("status", "reason", "plugin")
    search_fields = ("reason", "plugin", "tenant__name", "error")
    readonly_fields = (
        "reason",
        "plugin",
        "tenant",
        "actor",
        "status",
        "rendered_count",
        "synced_count",
        "pruned_count",
        "failed_count",
        "error",
        "started_at",
        "finished_at",
        "created_at",
    )

# Register your models here.
admin.site.register(Tenant, TenantAdmin)
admin.site.register(TenantBilling, TenantBillingAdmin)
admin.site.register(TenantLimit, TenantLimitAdmin)
admin.site.register(TenantUser, TenantUserAdmin)
admin.site.register(Node, NodeAdmin)
admin.site.register(NodeToken, NodeTokenAdmin)
admin.site.register(TenantUsage, TenantUsageAdmin)
admin.site.register(AuditLog, AuditLogAdmin)
admin.site.register(SupervisorVersion, SupervisorVersionAdmin)
admin.site.register(AlloyVersion, AlloyVersionAdmin)
admin.site.register(GitOpsResource, GitOpsResourceAdmin)
admin.site.register(GitOpsSyncRun, GitOpsSyncRunAdmin)
