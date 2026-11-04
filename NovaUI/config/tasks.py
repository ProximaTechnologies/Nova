import logging

from celery import shared_task
from celery.schedules import crontab
from django.conf import settings
from django.db.models import Count
from django.utils import timezone

from NovaUI.celery import app
from .models import Node, NodeToken, Tenant, TenantUsage

logger = logging.getLogger(__name__)


@app.on_after_finalize.connect
def setup_periodic_tasks(sender, **kwargs):
    logger.info("Setting up periodic Celery tasks.")
    sender.add_periodic_task(30.0, clean_token.s(), name="Clean expired tokens every 30 seconds")
    tenant_usage_interval = getattr(settings, "TENANT_USAGE_RECHECK_SECONDS", 300)
    sender.add_periodic_task(
        tenant_usage_interval,
        recheck_tenant_usage.s(),
        name=f"Recheck tenant usage every {tenant_usage_interval} seconds",
    )
    sender.add_periodic_task(
        crontab(hour=3, minute=0),
        sync_gitops_resources_task.s(reason="daily"),
        name="Sync GitOps resources daily",
    )


@shared_task
def clean_token():
    old_tokens = NodeToken.objects.filter(expires_at__lte=timezone.now())
    count = old_tokens.count()
    old_tokens.delete()
    logger.info("Deleted %s expired node tokens.", count)


@shared_task
def recheck_tenant_usage():
    node_counts = {
        row["tenant_id"]: row["node_count"]
        for row in Node.objects.values("tenant_id").annotate(node_count=Count("id"))
    }
    tenants_checked = 0
    created_count = 0
    updated_count = 0

    for tenant in Tenant.objects.iterator():
        tenants_checked += 1
        node_count = node_counts.get(tenant.pk, 0)
        usage = TenantUsage.objects.filter(tenant=tenant).order_by("-updated_at").first()
        if usage is None:
            TenantUsage.objects.create(tenant=tenant, nodes=node_count)
            created_count += 1
            continue
        if usage.nodes == node_count:
            continue

        usage.nodes = node_count
        usage.save(update_fields=["nodes", "updated_at"])
        updated_count += 1

    logger.info(
        "Rechecked tenant usage for %s tenants: %s created, %s updated.",
        tenants_checked,
        created_count,
        updated_count,
    )
    return {"tenants": tenants_checked, "created": created_count, "updated": updated_count}


@shared_task
def sync_gitops_resources_task(plugin_names=None, tenant_id=None, reason="manual"):
    from config.gitops.sync import sync_gitops_resources

    plugin_names = plugin_names or None
    run = sync_gitops_resources(plugin_names=plugin_names, tenant_id=tenant_id, reason=reason)
    return run.pk
