from django.urls import reverse
from urllib.parse import urlencode

from agent.provisioning import ensure_collector_auth
from config.audit import tenant_audit_entries
from config.models import (
    Node,
    NodeFeatures,
    NodeStatus,
    TenantLimit,
    TenantUsage,
    TenantUser,
    record_audit,
)
from config.file_monitors import sync_file_monitors
from config.forms import (
    NodeCreateForm,
    NodeForm,
    NodeFeaturesForm,
    NodeUpdateForm,
    TenantUserForm,
    build_file_monitor_formset,
    has_file_monitor_formset_data,
)
from config.grafana_urls import grafana_external_url
from config.integration import grafana_org_mappings_for_tenant
from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction, IntegrityError
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_http_methods
from django.contrib.auth.decorators import login_required
from django.contrib.admin.views.decorators import staff_member_required
from django.conf import settings
from django.http import Http404, HttpResponseBadRequest
from django.utils import timezone
from config.oidc_claims import tenant_claims_for_user
from .utils import get_tenant_from_secure_id, get_user_tenant, tenant_role_required
# Create your views here.


def _sync_apisix_consumers_after_commit(tenant, actor):
    tenant_id = tenant.pk

    def sync_apisix_consumers():
        from config.gitops.sync import sync_gitops_resources

        sync_gitops_resources(
            plugin_names=["apisix"],
            tenant_id=tenant_id,
            reason="collector_created",
            actor=actor,
        )

    transaction.on_commit(sync_apisix_consumers, robust=True)


def _resource_usage(label, used, limit, description):
    used = used or 0
    has_limit = limit is not None and limit > 0
    percent = min(round((used / limit) * 100), 100) if has_limit else 0
    return {
        "label": label,
        "used": used,
        "limit": limit,
        "limit_label": "-" if limit is None else limit,
        "limit_is_zero": limit == 0,
        "has_limit": has_limit,
        "percent": percent,
        "description": description,
    }


def _tenant_grafana_url(tenant):
    grafana_base_url = grafana_external_url()
    if not grafana_base_url:
        return ""
    if tenant and tenant.grafana_org_id:
        return f"{grafana_base_url}/?{urlencode({'orgId': tenant.grafana_org_id})}"
    return grafana_base_url


def _user_display(user):
    return user.email or user.get_username()


def _feature_summary(feature_rows, total_collectors):
    feature_fields = [
        ("Linux metrics", "linuxMonitor"),
        ("Windows metrics", "windowsMonitor"),
        ("Container metrics", "containerMonitor"),
        ("Journal logs", "journalLog"),
        ("Windows events", "windowsEventLog"),
        ("Docker logs", "dockerLogs"),
        ("File monitors", "fileMonitor"),
    ]
    summary = []
    for label, field_name in feature_fields:
        count = sum(1 for row in feature_rows if getattr(row, field_name))
        percent = min(round((count / total_collectors) * 100), 100) if total_collectors else 0
        summary.append({
            "label": label,
            "count": count,
            "percent": percent,
        })
    return summary


def _can_provision_collectors(request, role):
    return bool(role in {"admin", "editor"})


@login_required
def tenant_list(request):
    tu = TenantUser.objects.filter(user=request.user).all()
    tenants = []
    for i in tu:
        tenants.append(i.tenant)
    return render(request, 'ui/tenant_list.html', {'tenants': tenants, 'tenant_name': None})


@login_required
@tenant_role_required("admin", "editor", "viewer")
def dashboard(request, tenant_id_secure: str):
    tenant = request.tenant
    tenant_name = tenant.name
    role = request.tenant_user.role
    collectors_qs = Node.objects.filter(tenant=tenant).order_by("node_name")

    collectors = list(collectors_qs)
    total_collectors = len(collectors)
    status_counts = {status: 0 for status in NodeStatus.values}
    for collector in collectors:
        status_counts[collector.status] += 1

    enabled_collectors = total_collectors - status_counts[NodeStatus.DISABLED]
    disabled_collectors = status_counts[NodeStatus.DISABLED]
    nodes_online = status_counts[NodeStatus.ONLINE]
    nodes_warning = status_counts[NodeStatus.WARNING]
    nodes_offline = status_counts[NodeStatus.OFFLINE]
    nodes_never_connected = status_counts[NodeStatus.NEVER_CONNECTED]
    needs_attention = nodes_warning + nodes_offline + nodes_never_connected

    last_sync_candidates = [collector.effective_last_seen_at for collector in collectors if
                            collector.effective_last_seen_at]
    last_sync_at = max(last_sync_candidates) if last_sync_candidates else None
    recent_collectors = sorted(
        collectors,
        key=lambda collector: collector.effective_last_seen_at or collector.updated_at,
        reverse=True,
    )[:5]

    limits = TenantLimit.objects.filter(tenant=tenant).order_by("-updated_at").first() if tenant else None
    usage = TenantUsage.objects.filter(tenant=tenant).order_by("-updated_at").first() if tenant else None
    feature_rows = list(NodeFeatures.objects.filter(node__tenant=tenant)) if tenant else []

    nodes_used = usage.nodes if usage else None
    if nodes_used is None:
        nodes_used = total_collectors
    clusters_used = usage.clusters if usage else 0
    vcpus_used = usage.vcpus if usage else 0

    nodes_limit = limits.max_nodes if limits else None
    clusters_limit = limits.max_clusters if limits else None
    vcpus_limit = limits.max_vcpus if limits else None
    resource_usage = [
        _resource_usage("Collectors", nodes_used, nodes_limit, "Installed collector allowance"),
        _resource_usage("Clusters", clusters_used, clusters_limit, "Reported Kubernetes clusters"),
        _resource_usage("vCPUs", vcpus_used, vcpus_limit, "Reported cluster capacity"),
    ]
    status_cards = [
        {
            "key": "online",
            "label": "Online",
            "count": nodes_online,
            "badge_class": "badge-success",
            "description": "Checked in within 5 minutes",
        },
        {
            "key": "warning",
            "label": "Warning",
            "count": nodes_warning,
            "badge_class": "badge-warning",
            "description": "Last signal is 5-30 minutes old",
        },
        {
            "key": "offline",
            "label": "Offline",
            "count": nodes_offline,
            "badge_class": "badge-error",
            "description": "No signal for over 30 minutes",
        },
        {
            "key": "never_connected",
            "label": "Never connected",
            "count": nodes_never_connected,
            "badge_class": "badge-ghost",
            "description": "Installer has not checked in",
        },
    ]
    grafana_url = _tenant_grafana_url(tenant)

    context = {
        "title": "Dashboard",
        "total_collectors": total_collectors,
        "total_collectors_enabled": enabled_collectors,
        "collectors_disabled": disabled_collectors,
        "nodes_online": nodes_online,
        "nodes_warning": nodes_warning,
        "nodes_offline": nodes_offline,
        "nodes_never_connected": nodes_never_connected,
        "needs_attention": needs_attention,
        "status_counts": status_counts,
        "status_cards": status_cards,
        "last_sync_at": last_sync_at,
        "last_sync_at_iso": last_sync_at.isoformat() if last_sync_at else "",
        "recent_collectors": recent_collectors,
        "resource_usage": resource_usage,
        "feature_summary": _feature_summary(feature_rows, total_collectors),
        "nodes_used": nodes_used,
        "nodes_limit": nodes_limit,
        "clusters_used": clusters_used,
        "clusters_limit": clusters_limit,
        "vcpus_used": vcpus_used,
        "vcpus_limit": vcpus_limit,
        "grafana_url": grafana_url,
        "grafana_org_id": tenant.grafana_org_id if tenant else None,
        "grafana_configured": bool(grafana_url),
        "role": role,
        "billing_status": tenant.billing_status if settings.BILLING_ENABLED else None,
        "can_provision_collectors": _can_provision_collectors(request, role),
        "tenant_name": tenant_name,
        "tenant_id_secure": tenant_id_secure,
        "dashboard_ws_path": f"/ws/{tenant_id_secure}/dashboard/",
    }

    return render(request, 'ui/dashboard.html.j2', context)


@staff_member_required
def oidc_claims_debug(request, tenant_id_secure: str):
    tenant = get_tenant_from_secure_id(tenant_id_secure)
    tenant_user = TenantUser.objects.filter(user=request.user, tenant=tenant).first()
    role = tenant_user.role if tenant_user else None
    tenant_claims = tenant_claims_for_user(request.user, tenant=tenant)
    groups = tenant_claims["groups"]
    roles = tenant_claims["roles"]
    org_mapping = grafana_org_mappings_for_tenant(tenant)
    claims = {
        "email": request.user.email,
        "name": request.user.get_full_name() or request.user.get_username(),
        "groups": groups,
        "roles": roles,
    }
    return render(
        request,
        "ui/debug/oidc_claims.html.j2",
        {
            "title": "OIDC Claims Debug",
            "tenant": tenant,
            "tenant_name": tenant.name,
            "tenant_id_secure": tenant_id_secure,
            "role": role,
            "groups": groups,
            "roles": roles,
            "org_mapping": org_mapping,
            "claims": claims,
            "grafana_org_id": tenant.grafana_org_id,
        },
    )


@login_required
def collector_list(request, tenant_id_secure: str):
    tenant, tenant_name = get_user_tenant(request, tenant_id_secure)
    tenant_user = TenantUser.objects.get(user=request.user, tenant=tenant)
    role = tenant_user.role
    collectors = Node.objects.filter(tenant=tenant).order_by("node_name")

    context = {
        "title": "Collectors",
        "collectors": collectors,
        "role": role,
        "can_provision_collectors": _can_provision_collectors(request, role),
        "tenant_name": tenant_name,
        "tenant_id_secure": tenant_id_secure,
    }
    return render(request, 'ui/collectors.html.j2', context=context)


def _collector_add_form_context(
    request,
    tenant_id_secure,
    *,
    node_form=None,
    features_form=None,
    file_monitor_formset=None,
    selected_platform="",
):
    tenant = request.tenant
    node_form = node_form or NodeCreateForm()
    features_form = features_form or NodeFeaturesForm()
    if file_monitor_formset is None:
        file_monitor_formset = build_file_monitor_formset(
            tenant=tenant,
            feature_enabled=False,
        )
    return {
        "node_form": node_form,
        "features_form": features_form,
        "file_monitor_formset": file_monitor_formset,
        "file_monitor_limit": file_monitor_formset.file_monitor_limit,
        "selected_platform": selected_platform,
        "tenant_name": tenant.name,
        "tenant_id_secure": tenant_id_secure,
        "collector_list_url": reverse("list_collectors", kwargs={"tenant_id_secure": tenant_id_secure}),
    }


@tenant_role_required("admin", "editor")
@require_http_methods(["GET"])
def collector_add_platform_step(request, tenant_id_secure: str):
    return render(
        request,
        "ui/collectors/partials/add_platform_step.html.j2",
        _collector_add_form_context(request, tenant_id_secure),
    )


@tenant_role_required("admin", "editor")
@require_http_methods(["GET"])
def collector_add_features_step(request, tenant_id_secure: str):
    platform = request.GET.get("platform", "")
    if platform != "linux":
        return HttpResponseBadRequest("Only the Linux collector path is available.")
    return render(
        request,
        "ui/collectors/partials/add_features_step.html.j2",
        _collector_add_form_context(request, tenant_id_secure, selected_platform="linux"),
    )


@tenant_role_required("admin", "editor")
@require_http_methods(["GET", "POST"])
@transaction.atomic
def collector_add(request, tenant_id_secure: str):
    if not _can_provision_collectors(request, request.tenant_user.role):
        messages.info(request, "Collector provisioning is handled manually for now.")
        return redirect("list_collectors", tenant_id_secure=tenant_id_secure)
    if request.tenant.billing_suspended:
        messages.error(request, "Collector provisioning is disabled while billing is suspended.")
        return redirect("list_collectors", tenant_id_secure=tenant_id_secure)

    node_form = NodeCreateForm(request.POST or None)
    features_form = NodeFeaturesForm(request.POST or None)
    selected_platform = request.POST.get("platform", "") if request.method == "POST" else ""

    tenant = request.tenant
    current_usage = TenantUsage.objects.filter(tenant=tenant).order_by("-updated_at").first()
    current_limit = TenantLimit.objects.filter(tenant=tenant).order_by("-updated_at").first()
    if current_usage is None:
        current_usage = TenantUsage.objects.create(tenant=tenant)
    if current_limit is None:
        messages.error(request, "Collector limit is not configured for this team.")
        return redirect("list_collectors", tenant_id_secure=tenant_id_secure)

    # return an error if the limit is reached
    if current_usage.nodes >= current_limit.max_nodes:
        messages.error(request, "Collector limit reached.")
        return redirect("list_collectors", tenant_id_secure=tenant_id_secure)

    file_monitor_feature_enabled = request.POST.get("fileMonitor") == "on" if request.method == "POST" else False
    file_monitor_data = (
        request.POST
        if request.method == "POST" and (has_file_monitor_formset_data(request.POST) or file_monitor_feature_enabled)
        else None
    )
    file_monitor_formset = build_file_monitor_formset(
        data=file_monitor_data,
        tenant=tenant,
        feature_enabled=file_monitor_feature_enabled,
    )
    file_monitor_formset_valid = (
        file_monitor_formset.is_valid()
        if request.method == "POST" and (file_monitor_data is not None or file_monitor_feature_enabled)
        else True
    )
    platform_supported = selected_platform == "linux" if request.method == "POST" else True
    if request.method == "POST" and not platform_supported:
        features_form.add_error(None, "Choose Linux to continue.")

    if (
        request.method == "POST"
        and platform_supported
        and node_form.is_valid()
        and features_form.is_valid()
        and file_monitor_formset_valid
    ):
        tenantUserObj = TenantUser.objects.get(user=request.user, tenant=tenant)
        node_form.tenant = tenantUserObj.tenant.id
        node = node_form.save(commit=False)
        node.tenant = tenantUserObj.tenant
        node.enabled = True
        node.save()
        features = features_form.save(commit=False)
        features.node = node
        features.save()
        sync_file_monitors(
            node,
            file_monitor_formset.cleaned_specs if file_monitor_data is not None else [],
            actor=request.user,
            require_enabled=features.fileMonitor,
        )
        ensure_collector_auth(node)
        _ = node.generate_token()  # returns NodeToken object
        _sync_apisix_consumers_after_commit(tenant, request.user)
        # update usage
        current_usage.nodes += 1
        current_usage.save()
        messages.success(request, "Collector created.")
        return redirect("run_script_collector", tenant_id_secure=tenant_id_secure, node_id=node.node_id)

    return render(
        request,
        "ui/collectors/add.html.j2",
        _collector_add_form_context(
            request,
            tenant_id_secure,
            node_form=node_form,
            features_form=features_form,
            file_monitor_formset=file_monitor_formset,
            selected_platform=selected_platform,
        ),
    )


@tenant_role_required("admin", "editor")
@require_http_methods(["GET", "POST"])
def collector_run_script(request, node_id, tenant_id_secure: str):
    tenant = request.tenant
    tenant_name = tenant.name
    role = request.tenant_user.role
    node = get_object_or_404(Node, tenant=tenant, node_id=node_id)
    if request.method == "POST":
        try:
            node.generate_token(actor=request.user)
        except ValidationError as exc:
            messages.error(request, exc.messages[0] if exc.messages else "Unable to generate installer token.")
        else:
            messages.success(request, "Generated a new one-time installer token.")
        return redirect("run_script_collector", tenant_id_secure=tenant_id_secure, node_id=node.node_id)

    token = (
        node.tokens.filter(used_at__isnull=True, revoked_at__isnull=True, expires_at__gt=timezone.now())
        .order_by("-created_at")
        .first()
    )
    return render(request, "ui/collectors/run_script.html.j2",
                  {
                      "title": "Install Collector",
                      "node": node,
                      "token": token,
                      "ui_url": settings.UI_URL,
                      "role": role,
                      "tenant_name": tenant_name,
                      "tenant_id_secure": tenant_id_secure,
                  })


@tenant_role_required("admin", "editor")
@require_http_methods(["GET", "POST"])
@transaction.atomic
def collector_edit(request, tenant_id_secure: str, node_id=None):
    node = get_object_or_404(Node, tenant=request.tenant, node_id=node_id)

    features = get_object_or_404(NodeFeatures, node=node)
    tenant_name = request.tenant.name
    role = request.tenant_user.role

    if request.method == "POST":
        was_enabled = node.enabled
        node_form = NodeForm(request.POST, instance=node)
        update_form = NodeUpdateForm(request.POST, instance=node, prefix="updates")
        features_form = NodeFeaturesForm(request.POST, instance=features)
        file_monitor_feature_enabled = request.POST.get("fileMonitor") == "on"
        file_monitor_data = (
            request.POST
            if has_file_monitor_formset_data(request.POST) or file_monitor_feature_enabled
            else None
        )
        file_monitor_formset = build_file_monitor_formset(
            data=file_monitor_data,
            tenant=request.tenant,
            node=node,
            feature_enabled=file_monitor_feature_enabled,
        )
        file_monitor_formset_valid = (
            file_monitor_formset.is_valid()
            if file_monitor_data is not None or file_monitor_feature_enabled
            else True
        )

        if (
                node_form.is_valid()
                and update_form.is_valid()
                and features_form.is_valid()
                and file_monitor_formset_valid
        ):
            node = node_form.save()
            node = update_form.save()
            features = features_form.save()
            sync_file_monitors(
                node,
                file_monitor_formset.cleaned_specs if file_monitor_data is not None else [],
                actor=request.user,
                require_enabled=features.fileMonitor,
            )
            if was_enabled != node.enabled:
                _sync_apisix_consumers_after_commit(request.tenant, request.user)
            messages.success(request, "Collector updated.")
            return redirect(reverse("list_collectors", args=[tenant_id_secure]))

    else:
        node_form = NodeForm(instance=node)
        update_form = NodeUpdateForm(instance=node, prefix="updates")
        features_form = NodeFeaturesForm(instance=features)
        file_monitor_formset = build_file_monitor_formset(
            tenant=request.tenant,
            node=node,
            feature_enabled=features.fileMonitor,
        )

    return render(
        request,
        "ui/collectors/node_edit.html.j2",
        {
            "title": "Edit Collector",
            "node_form": node_form,
            "update_form": update_form,
            "features_form": features_form,
            "file_monitor_formset": file_monitor_formset,
            "file_monitor_limit": file_monitor_formset.file_monitor_limit,
            "node": node,
            "features": features,
            "role": role,
            "tenant_name": tenant_name,
            "tenant_id_secure": tenant_id_secure,
            "collector_list_url": reverse("list_collectors", kwargs={"tenant_id_secure": tenant_id_secure}),
        },
    )


@tenant_role_required("admin")
@require_http_methods(["GET", "POST"])
def collector_delete(request, tenant_id_secure: str, node_id=None):
    node = get_object_or_404(Node, tenant=request.tenant, node_id=node_id)
    current_usage = TenantUsage.objects.filter(tenant=node.tenant).order_by("-updated_at").first()
    if request.method == "POST":
        with transaction.atomic():
            node.delete()  # NodeFeatures goes via CASCADE
            if current_usage is not None:
                current_usage.nodes = max(current_usage.nodes - 1, 0)
                current_usage.save(update_fields=["nodes", "updated_at"])
            _sync_apisix_consumers_after_commit(request.tenant, request.user)
        messages.success(request, "Collector deleted.")
        return redirect("list_collectors", tenant_id_secure=tenant_id_secure)
    return render(
        request,
        "ui/collectors/delete.html.j2",
        {
            "title": "Delete Collector",
            "object": node,
            "role": request.tenant_user.role,
            "tenant_name": request.tenant.name,
            "tenant_id_secure": tenant_id_secure,
        },
    )


@tenant_role_required("admin", "editor")
@require_http_methods(["GET", "POST"])
def collectors_onboarding(request, tenant_id_secure: str):
    pass


@login_required
def settings_profile(request, tenant_id_secure: str):
    pass


@tenant_role_required("admin")
def settings_team(request, tenant_id_secure):
    tenant = request.tenant
    tenant_name = tenant.name
    user_role = request.tenant_user.role
    members = TenantUser.objects.filter(tenant=tenant).select_related("user").order_by("user__username")
    return render(request, "ui/settings/teams/list.html",
                  {
                      "members": members,
                      "current_user": request.user,
                      "role": user_role,
                      "title": "Team Members",
                      "tenant_name": tenant_name,
                      "tenant_id_secure": tenant_id_secure,
                  })


@tenant_role_required("admin")
def settings_team_add(request, tenant_id_secure: str):
    tenant = request.tenant
    tenant_name = tenant.name

    if request.method == "POST":
        form = TenantUserForm(request.POST, tenant=tenant)
        if form.is_valid():
            # Optional fast-path: block users who already belong to ANY tenant
            resolved_user = getattr(form, "_resolved_user", None)
            if resolved_user and TenantUser.objects.filter(user=resolved_user, tenant=tenant).exists():
                form.add_error(None, "That account already belongs to this team.")
            else:
                tu = form.save(commit=False)
                tu.tenant = tenant
                try:
                    with transaction.atomic():
                        tu.save()
                    messages.success(request, f"Added {_user_display(tu.user)} as {tu.get_role_display()}.")
                    return redirect("settings_team", tenant_id_secure=tenant_id_secure)
                except IntegrityError:
                    # Covers both "already in this team" and "already in another team"
                    form.add_error(None, "That account is already part of a team.")
    else:
        form = TenantUserForm(tenant=tenant)

    return render(
        request,
        "ui/settings/teams/form.html",
        {"form": form,
         "title": "Add team member",
         "submit_label": "Add",
         "tenant_name": tenant_name,
         "tenant_id_secure": tenant_id_secure,
         },
    )


@tenant_role_required("admin")
def settings_team_edit(request, tenant_id_secure, tu_id=None):
    tenant = request.tenant
    tenant_name = tenant.name
    # team_id is the PK of TenantUser
    tu = get_object_or_404(TenantUser, pk=tu_id, tenant=tenant)
    original_user = tu.user

    if request.method == "POST":
        # Only allow editing role; keep user fixed by binding instance (the form still shows both fields per your request)
        form = TenantUserForm(request.POST, instance=tu, tenant=tenant)
        if form.is_valid():
            tu = form.save(commit=False)
            tu.tenant = tenant  # enforce safety
            tu.user = original_user  # keep original user
            tu.save()
            messages.success(request, f"Updated {_user_display(tu.user)} to {tu.get_role_display()}.")
            return redirect("settings_team", tenant_id_secure)
    else:
        form = TenantUserForm(instance=tu, tenant=tenant)

    return render(request, "ui/settings/teams/form.html",
                  {
                      "form": form,
                      "title": "Edit team member",
                      "submit_label": "Save",
                      "tenant_name": tenant_name,
                      "tenant_id_secure": tenant_id_secure,
                  })


@tenant_role_required("admin")
def settings_team_delete(request, tenant_id_secure, tu_id=None):
    tenant = request.tenant
    tenant_name = tenant.name
    tu = get_object_or_404(TenantUser, pk=tu_id, tenant=tenant)

    if request.method == "POST":
        user_display = _user_display(tu.user)
        tu.delete()
        messages.success(request, f"Removed {user_display} from the team.")
        return redirect("settings_team", tenant_id_secure=tenant_id_secure)

    return render(
        request,
        "ui/settings/teams/confirm_delete.html",
        {
            "member": tu,
            "title": "Remove Member",
            "role": request.tenant_user.role,
            "tenant_name": tenant_name,
            "tenant_id_secure": tenant_id_secure,
        },
    )


@tenant_role_required("admin")
def settings_team_limits(request, tenant_id_secure):
    tenant = request.tenant
    tenant_name = tenant.name
    tu = request.tenant_user
    can_edit_limits = request.user.is_staff

    # Load current limits (latest by updated_at)
    current_limit = TenantLimit.objects.filter(tenant=tenant).order_by("-updated_at").first()
    # Load current usage (latest by updated_at)
    current_usage = TenantUsage.objects.filter(tenant=tenant).order_by("-updated_at").first()

    if request.method == "POST":
        if not can_edit_limits:
            raise PermissionDenied

        # Basic manual validation without a dedicated Form for now
        def to_non_negative_int(value, field_name):
            try:
                v = int(value)
                if v < 0:
                    raise ValueError
                return v
            except (TypeError, ValueError):
                raise ValueError(f"{field_name} must be a non-negative integer.")

        try:
            max_nodes = to_non_negative_int(request.POST.get("max_nodes", ""), "Max nodes")
            max_clusters = to_non_negative_int(request.POST.get("max_clusters", ""), "Max clusters")
            max_vcpus = to_non_negative_int(request.POST.get("max_vcpus", ""), "Max vCPUs")

            # Users cannot set or change billing. If limits record doesn't exist or has no billing.
            if not current_limit or not getattr(current_limit, "billing_id", None):
                raise ValueError("Billing plan is not configured for this team. Please contact support.")

            # Ensure new limits are not below current usage
            if current_usage:
                if max_nodes < current_usage.nodes:
                    raise ValueError(f"Max nodes cannot be lower than current usage ({current_usage.nodes}).")
                if max_clusters < current_usage.clusters:
                    raise ValueError(f"Max clusters cannot be lower than current usage ({current_usage.clusters}).")
                if max_vcpus < current_usage.vcpus:
                    raise ValueError(f"Max vCPUs cannot be lower than current usage ({current_usage.vcpus}).")

            with transaction.atomic():
                current_limit.max_nodes = max_nodes
                current_limit.max_clusters = max_clusters
                current_limit.max_vcpus = max_vcpus
                current_limit.save()
                record_audit(
                    "tenant_limit.updated",
                    "TenantLimit",
                    actor=request.user,
                    tenant=tenant,
                    target_id=current_limit.pk,
                    metadata={
                        "max_nodes": max_nodes,
                        "max_clusters": max_clusters,
                        "max_vcpus": max_vcpus,
                    },
                )

            messages.success(request, "Team limits updated successfully.")
            return redirect("settings_team_limits", tenant_id_secure)

        except ValueError as ve:
            messages.error(request, str(ve))

    # GET or fallthrough after validation error: render the form
    return render(
        request,
        "ui/settings/limits/edit.html.j2",
        {
            "title": "Team Limits",
            "limit": current_limit,
            "role": tu.role,
            "tenant": tenant,
            "tenant_name": tenant_name,
            "tenant_id_secure": tenant_id_secure,
            "can_edit_limits": can_edit_limits,
        },
    )


@tenant_role_required("admin")
def settings_billing(request, tenant_id_secure: str):
    if not settings.BILLING_ENABLED:
        raise Http404("Billing is disabled.")
    tenant = request.tenant
    current_limit = TenantLimit.objects.filter(tenant=tenant).select_related("billing").order_by("-updated_at").first()
    billing = current_limit.billing if current_limit and current_limit.billing_id else None

    node_subtotal = cluster_subtotal = vcpu_subtotal = total_cost = None
    if billing and current_limit:
        node_subtotal = billing.price_per_node * current_limit.max_nodes
        cluster_subtotal = billing.price_per_cluster * current_limit.max_clusters
        vcpu_subtotal = billing.price_per_vcpu * current_limit.max_vcpus
        total_cost = node_subtotal + cluster_subtotal + vcpu_subtotal

    return render(
        request,
        "ui/settings/billing.html.j2",
        {
            "title": "Billing",
            "tenant": tenant,
            "tenant_name": tenant.name,
            "tenant_id_secure": tenant_id_secure,
            "role": request.tenant_user.role,
            "limit": current_limit,
            "billing": billing,
            "node_subtotal": node_subtotal,
            "cluster_subtotal": cluster_subtotal,
            "vcpu_subtotal": vcpu_subtotal,
            "total_cost": total_cost,
        },
    )


@tenant_role_required("admin")
def settings_audit(request, tenant_id_secure: str):
    tenant = request.tenant
    entries = tenant_audit_entries(tenant)[:100]
    return render(
        request,
        "ui/settings/audit.html.j2",
        {
            "title": "Audit Log",
            "tenant": tenant,
            "tenant_name": tenant.name,
            "tenant_id_secure": tenant_id_secure,
            "role": request.tenant_user.role,
            "entries": entries,
        },
    )
