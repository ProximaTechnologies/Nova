from django.contrib import admin
from .models import CollectorAuth


@admin.register(CollectorAuth)
class CollectorAuthAdmin(admin.ModelAdmin):
    list_display = ("node_name", "node_tenant", "provisioner_backend", "provisioned_at", "updated_at")
    search_fields = (
        "node__node_name",
        "node__node_id",
        "node__tenant__name",
        "config_consumer_name",
        "mimir_consumer_name",
        "loki_consumer_name",
    )
    readonly_fields = (
        "node",
        "config_consumer_name",
        "mimir_consumer_name",
        "loki_consumer_name",
        "provisioner_backend",
        "provisioned_at",
        "created_at",
        "updated_at",
    )
    exclude = ("config_password", "mimir_password", "loki_password", "tempo_password")

    def has_add_permission(self, request):
        return False

    def node_name(self, obj):
        return obj.node.node_name

    def node_tenant(self, obj):
        return obj.node.tenant.name
