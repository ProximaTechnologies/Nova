import yaml
from django.apps import apps
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from config.audit import record_audit

from .forgejo import ForgejoClient, bootstrap_tenant_gitops, forgejo_bootstrap_enabled, tenant_gitops_repo_names
from .registry import filter_plugins, load_plugins
from .writer import GitOpsRepositoryNotConfigured, GitOpsWriter

GITOPS_RESOURCE_RENDERED = "rendered"
GITOPS_RESOURCE_SYNCED = "synced"
GITOPS_RESOURCE_FAILED = "failed"
GITOPS_RESOURCE_PRUNED = "pruned"
GITOPS_SYNC_COMPLETED = "completed"
GITOPS_SYNC_FAILED = "failed"


def _config_model(model_name):
    return apps.get_model("config", model_name)


def sync_gitops_resources(*, plugin_names=None, tenant_id=None, reason="manual", actor=None, write=True):
    Tenant = _config_model("Tenant")
    GitOpsSyncRun = _config_model("GitOpsSyncRun")
    tenant = Tenant.objects.filter(pk=tenant_id).first() if tenant_id else None
    if isinstance(plugin_names, str):
        selected_plugins = [plugin_names]
    else:
        selected_plugins = list(plugin_names or [])
    available_plugins = load_plugins()
    plugins = filter_plugins(available_plugins, selected_plugins)
    if selected_plugins:
        matched_names = {plugin.name for plugin in plugins}
        missing_names = sorted(set(selected_plugins) - matched_names)
        if missing_names:
            raise ValueError(f"No GitOps plugins matched: {', '.join(missing_names)}")
    run = GitOpsSyncRun.objects.create(
        reason=reason,
        plugin=",".join(plugin.name for plugin in plugins),
        tenant=tenant,
        actor=actor if getattr(actor, "is_authenticated", False) else None,
    )

    rendered_resources = []
    try:
        for plugin in plugins:
            rendered_resources.extend(plugin.desired_resources(tenant=tenant))

        rendered_rows = _persist_desired_resources(rendered_resources)
        pruned_rows = _mark_stale_resources_pruned(
            plugins=[plugin.name for plugin in plugins],
            desired_keys={resource.logical_key for resource in rendered_resources},
            tenant=tenant,
        )

        synced_count = 0
        if write:
            synced_count = _write_resources(rendered_rows, pruned_rows, reason=reason)

        now = timezone.now()
        run.status = GITOPS_SYNC_COMPLETED
        run.rendered_count = len(rendered_rows)
        run.synced_count = synced_count
        run.pruned_count = len(pruned_rows)
        run.finished_at = now
        run.save(update_fields=["status", "rendered_count", "synced_count", "pruned_count", "finished_at"])
        record_audit(
            "gitops.sync_completed",
            "GitOpsSyncRun",
            actor=actor,
            tenant=tenant,
            target_id=run.pk,
            metadata={
                "reason": reason,
                "plugins": [plugin.name for plugin in plugins],
                "rendered": len(rendered_rows),
                "synced": synced_count,
                "pruned": len(pruned_rows),
                "wrote_git": bool(write and _git_write_enabled()),
            },
        )
        return run
    except Exception as exc:
        now = timezone.now()
        run.status = GITOPS_SYNC_FAILED
        run.failed_count = len(rendered_resources)
        run.error = str(exc)
        run.finished_at = now
        run.save(update_fields=["status", "failed_count", "error", "finished_at"])
        record_audit(
            "gitops.sync_failed",
            "GitOpsSyncRun",
            actor=actor,
            tenant=tenant,
            target_id=run.pk,
            metadata={"reason": reason, "error": str(exc)},
        )
        raise


def _persist_desired_resources(resources):
    GitOpsResource = _config_model("GitOpsResource")
    now = timezone.now()
    rows = []
    with transaction.atomic():
        for desired in resources:
            row, _ = GitOpsResource.objects.update_or_create(
                plugin=desired.plugin,
                logical_key=desired.logical_key,
                defaults={
                    "tenant": desired.tenant,
                    "api_version": desired.api_version,
                    "kind": desired.kind,
                    "namespace": desired.namespace,
                    "name": desired.name,
                    "manifest_path": desired.relative_path,
                    "desired_manifest": desired.manifest,
                    "desired_hash": desired.desired_hash,
                    "sync_status": GITOPS_RESOURCE_RENDERED,
                    "last_error": "",
                    "last_rendered_at": now,
                },
            )
            rows.append(row)
    return rows


def _mark_stale_resources_pruned(*, plugins, desired_keys, tenant):
    if not plugins:
        return []

    GitOpsResource = _config_model("GitOpsResource")
    stale = GitOpsResource.objects.filter(plugin__in=plugins).exclude(logical_key__in=desired_keys)
    if tenant is not None:
        stale = stale.filter(tenant=tenant)

    rows = list(stale.exclude(sync_status=GITOPS_RESOURCE_PRUNED))
    now = timezone.now()
    for row in rows:
        row.desired_manifest = {}
        row.desired_hash = ""
        row.sync_status = GITOPS_RESOURCE_PRUNED
        row.last_error = ""
        row.last_rendered_at = now
        row.save(update_fields=["desired_manifest", "desired_hash", "sync_status", "last_error", "last_rendered_at"])
    return rows


def _write_resources(rendered_rows, pruned_rows, *, reason):
    if forgejo_bootstrap_enabled():
        return _write_resources_to_forgejo(rendered_rows, pruned_rows, reason=reason)
    return _write_resources_to_worktree(rendered_rows, pruned_rows, reason=reason)


def _write_resources_to_worktree(rendered_rows, pruned_rows, *, reason):
    try:
        writer = GitOpsWriter()
    except GitOpsRepositoryNotConfigured:
        return 0

    now = timezone.now()
    synced_count = 0
    failed_rows = []
    for row in rendered_rows:
        try:
            writer.write_resource(row)
            row.last_applied_hash = row.desired_hash
            row.sync_status = GITOPS_RESOURCE_SYNCED
            row.last_error = ""
            row.last_synced_at = now
            row.save(update_fields=["last_applied_hash", "sync_status", "last_error", "last_synced_at"])
            synced_count += 1
        except Exception as exc:
            failed_rows.append((row, exc))

    for row in pruned_rows:
        try:
            writer.delete_resource(row)
            row.last_applied_hash = ""
            row.last_error = ""
            row.last_synced_at = now
            row.save(update_fields=["last_applied_hash", "last_error", "last_synced_at"])
        except Exception as exc:
            failed_rows.append((row, exc))

    if failed_rows:
        for row, exc in failed_rows:
            row.sync_status = GITOPS_RESOURCE_FAILED
            row.last_error = str(exc)
            row.save(update_fields=["sync_status", "last_error"])
        errors = "; ".join(f"{row}: {exc}" for row, exc in failed_rows)
        raise RuntimeError(errors)

    writer.commit(f"NovaUI GitOps sync: {reason}")
    return synced_count


def _write_resources_to_forgejo(rendered_rows, pruned_rows, *, reason):
    client = ForgejoClient()
    now = timezone.now()
    synced_count = 0
    failed_rows = []

    for row in rendered_rows:
        try:
            repo_name = _tenant_forgejo_repo_name(row.tenant, client)
            client.write_file(
                repo_name,
                _forgejo_resource_path(row),
                _to_yaml(row.desired_manifest),
                f"NovaUI GitOps sync: {reason}",
            )
            row.last_applied_hash = row.desired_hash
            row.sync_status = GITOPS_RESOURCE_SYNCED
            row.last_error = ""
            row.last_synced_at = now
            row.save(update_fields=["last_applied_hash", "sync_status", "last_error", "last_synced_at"])
            synced_count += 1
        except Exception as exc:
            failed_rows.append((row, exc))

    for row in pruned_rows:
        try:
            repo_name = _tenant_forgejo_repo_name(row.tenant, client)
            client.delete_file(
                repo_name,
                _forgejo_resource_path(row),
                f"NovaUI GitOps prune: {reason}",
            )
            row.last_applied_hash = ""
            row.last_error = ""
            row.last_synced_at = now
            row.save(update_fields=["last_applied_hash", "last_error", "last_synced_at"])
        except Exception as exc:
            failed_rows.append((row, exc))

    if failed_rows:
        for row, exc in failed_rows:
            row.sync_status = GITOPS_RESOURCE_FAILED
            row.last_error = str(exc)
            row.save(update_fields=["sync_status", "last_error"])
        errors = "; ".join(f"{row}: {exc}" for row, exc in failed_rows)
        raise RuntimeError(errors)

    return synced_count


def _tenant_forgejo_repo_name(tenant, client):
    if tenant is None:
        repo_name = getattr(settings, "GITOPS_FORGEJO_NOVA_REPO_NAME", "nova").strip() or "nova"
        org = getattr(settings, "GITOPS_FORGEJO_ORG", "").strip()
        if "/" in repo_name or not org:
            return repo_name
        return f"{org}/{repo_name}"

    configured_nova_repo = tenant_gitops_repo_names(tenant)[0]
    current_repo_name = (tenant.gitops_nova_repo or "").rsplit("/", 1)[-1]
    if not tenant.gitops_nova_repo or current_repo_name != configured_nova_repo:
        repositories = bootstrap_tenant_gitops(tenant, client=client)
        if repositories is not None:
            return repositories.nova.full_name or repositories.nova.name

    return tenant.gitops_nova_repo or configured_nova_repo


def _forgejo_resource_path(resource):
    return resource.manifest_path


def _to_yaml(manifest):
    return yaml.safe_dump(manifest, sort_keys=False, explicit_start=True)


def _git_write_enabled():
    return forgejo_bootstrap_enabled() or bool(settings.GITOPS_REPO_PATH)
