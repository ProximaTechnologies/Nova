import secrets
import time
import re
from datetime import timedelta

from auditlog.models import AbstractLogEntry, LogEntryManager
from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.core.validators import RegexValidator
from django.db import models, transaction
from django.utils import timezone

from config.gitops.resources import tenant_observability_id
from config.redis_client import set_json, set_string
import uuid
from agent.utils import generate_password

from django.db.models.signals import pre_delete, post_delete, post_save
from django.dispatch import receiver

# Create your models here.
def generate_prefix():
    return generate_password(10)


def generate_node_token():
    return secrets.token_urlsafe(32)


def default_node_token_expiry():
    ttl_minutes = getattr(settings, "NODE_TOKEN_TTL_MINUTES", 10)
    return timezone.now() + timedelta(minutes=ttl_minutes)


class TenantBillingStatus(models.TextChoices):
    ACTIVE = "active", "Active"
    GRACE = "grace", "Grace"
    RESTRICTED = "restricted", "Restricted"
    SUSPENDED = "suspended", "Suspended"


class NodeStatus(models.TextChoices):
    NEVER_CONNECTED = "never_connected", "Never connected"
    ONLINE = "online", "Online"
    WARNING = "warning", "Warning"
    OFFLINE = "offline", "Offline"
    DISABLED = "disabled", "Disabled"


class GitOpsResourceStatus(models.TextChoices):
    RENDERED = "rendered", "Rendered"
    SYNCED = "synced", "Synced"
    FAILED = "failed", "Failed"
    PRUNED = "pruned", "Pruned"


class GitOpsSyncRunStatus(models.TextChoices):
    RUNNING = "running", "Running"
    COMPLETED = "completed", "Completed"
    FAILED = "failed", "Failed"


SUPERVISOR_SEMVER_MESSAGE = (
    "Enter a SemVer version like 1.2.3, 1.2.3-rc.1, or 1.2.3+build.4. Do not include a leading v."
)
SUPERVISOR_IDENTIFIER_CHARS = set("0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz-")


sha256_validator = RegexValidator(
    regex=r"^[0-9a-fA-F]{64}$",
    message="Enter a 64-character hexadecimal SHA-256 checksum.",
)

ALLOY_STABLE_SEMVER_RE = (
    r"^v?"
    r"(0|[1-9]\d*)\."
    r"(0|[1-9]\d*)\."
    r"(0|[1-9]\d*)"
    r"(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)


def is_supervisor_semver(value):
    version = str(value or "").strip()
    if not version or len(version) > 50 or version.startswith("v"):
        return False

    core_and_prerelease, separator, build = version.partition("+")
    if separator and not _valid_semver_identifiers(build, allow_numeric_leading_zero=True):
        return False

    core, separator, prerelease = core_and_prerelease.partition("-")
    if separator and not _valid_semver_identifiers(prerelease, allow_numeric_leading_zero=False):
        return False

    core_parts = core.split(".")
    return len(core_parts) == 3 and all(_valid_semver_number(part) for part in core_parts)


def validate_supervisor_semver(value):
    if not is_supervisor_semver(value):
        raise ValidationError(SUPERVISOR_SEMVER_MESSAGE)


def _valid_semver_number(value):
    return value == "0" or (value.isdigit() and not value.startswith("0"))


def _valid_semver_identifiers(value, *, allow_numeric_leading_zero):
    identifiers = value.split(".")
    if not identifiers or any(not identifier for identifier in identifiers):
        return False
    for identifier in identifiers:
        if any(char not in SUPERVISOR_IDENTIFIER_CHARS for char in identifier):
            return False
        if not allow_numeric_leading_zero and identifier.isdigit() and not _valid_semver_number(identifier):
            return False
    return True


supervisor_semver_validator = validate_supervisor_semver


def create_tenant(*args, **kwargs):
    from .integration import create_tenant as _create_tenant

    return _create_tenant(*args, **kwargs)


def update_grafana_org_mapping(*args, **kwargs):
    from .integration import update_grafana_org_mapping as _update_grafana_org_mapping

    return _update_grafana_org_mapping(*args, **kwargs)


def delete_tenant(*args, **kwargs):
    from .integration import delete_tenant as _delete_tenant

    return _delete_tenant(*args, **kwargs)


def logout_user_from_grafana_using_email(*args, **kwargs):
    from .integration import logout_user_from_grafana_using_email as _logout_user_from_grafana_using_email

    return _logout_user_from_grafana_using_email(*args, **kwargs)


def normalize_alloy_version(value):
    version = str(value or "").strip()
    if not version or version.startswith("v"):
        return version
    if re.fullmatch(ALLOY_STABLE_SEMVER_RE, version):
        return f"v{version}"
    return version


class PackageFormat(models.TextChoices):
    DEB = "deb", "deb"
    RPM = "rpm", "rpm"


class SupervisorVersion(models.Model):
    version = models.CharField(max_length=50, unique=True, validators=[supervisor_semver_validator])
    enabled = models.BooleanField(default=True)
    notes = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at", "version"]

    def __str__(self):
        return self.version

    def save(self, *args, **kwargs):
        self.version = str(self.version or "").strip()
        self.full_clean()
        super().save(*args, **kwargs)


class SupervisorVersionArtifact(models.Model):
    version = models.ForeignKey(SupervisorVersion, on_delete=models.CASCADE, related_name="artifacts")
    arch = models.CharField(max_length=20)
    sha256 = models.CharField(max_length=64, validators=[sha256_validator])
    asset_url = models.URLField(max_length=500, blank=True)
    enabled = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["version", "arch"], name="uniq_supervisor_version_artifact_arch"),
        ]
        ordering = ["version", "arch"]

    def save(self, *args, **kwargs):
        self.sha256 = self.sha256.lower()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.version.version} {self.arch}"


class AlloyVersion(models.Model):
    version = models.CharField(max_length=50, unique=True)
    enabled = models.BooleanField(default=True)
    notes = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at", "version"]

    def __str__(self):
        return self.version

    def save(self, *args, **kwargs):
        self.version = normalize_alloy_version(self.version)
        super().save(*args, **kwargs)


class AlloyVersionArtifact(models.Model):
    version = models.ForeignKey(AlloyVersion, on_delete=models.CASCADE, related_name="artifacts")
    os = models.CharField(max_length=20, default="linux")
    arch = models.CharField(max_length=20)
    package_format = models.CharField(max_length=20, choices=PackageFormat.choices)
    sha256 = models.CharField(max_length=64, validators=[sha256_validator])
    package_url = models.URLField(max_length=500, blank=True)
    enabled = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["version", "os", "arch", "package_format"],
                name="uniq_alloy_version_artifact_platform",
            ),
        ]
        ordering = ["version", "os", "arch", "package_format"]

    def save(self, *args, **kwargs):
        self.os = self.os.lower()
        self.arch = self.arch.lower()
        self.package_format = self.package_format.lower()
        self.sha256 = self.sha256.lower()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.version.version} {self.os}/{self.arch}.{self.package_format}"


class AuditLog(models.Model):
    actor = models.ForeignKey("auth.User", null=True, blank=True, on_delete=models.SET_NULL)
    tenant = models.ForeignKey("config.Tenant", null=True, blank=True, on_delete=models.SET_NULL)
    action = models.CharField(max_length=100)
    target_type = models.CharField(max_length=100)
    target_id = models.CharField(max_length=255, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["tenant", "created_at"]),
            models.Index(fields=["action", "created_at"]),
        ]

    def __str__(self):
        return f"{self.action} {self.target_type}:{self.target_id}"


def record_audit(action, target_type, *, actor=None, tenant=None, target_id="", metadata=None):
    return AuditLog.objects.create(
        actor=actor if getattr(actor, "is_authenticated", False) else None,
        tenant=tenant,
        action=action,
        target_type=target_type,
        target_id=str(target_id or ""),
        metadata=metadata or {},
    )


class GitOpsResource(models.Model):
    tenant = models.ForeignKey("config.Tenant", null=True, blank=True, on_delete=models.SET_NULL)
    plugin = models.CharField(max_length=100)
    logical_key = models.CharField(max_length=255)
    api_version = models.CharField(max_length=100)
    kind = models.CharField(max_length=100)
    namespace = models.CharField(max_length=100)
    name = models.CharField(max_length=253)
    manifest_path = models.CharField(max_length=500)
    desired_manifest = models.JSONField(default=dict, blank=True)
    desired_hash = models.CharField(max_length=64, blank=True)
    last_applied_hash = models.CharField(max_length=64, blank=True)
    sync_status = models.CharField(
        max_length=20,
        choices=GitOpsResourceStatus.choices,
        default=GitOpsResourceStatus.RENDERED,
    )
    last_error = models.TextField(blank=True)
    last_rendered_at = models.DateTimeField(null=True, blank=True)
    last_synced_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["plugin", "logical_key"], name="uniq_gitops_resource_logical_key"),
        ]
        indexes = [
            models.Index(fields=["tenant", "plugin"]),
            models.Index(fields=["plugin", "kind", "namespace", "name"]),
            models.Index(fields=["sync_status", "updated_at"]),
        ]
        ordering = ["plugin", "namespace", "kind", "name"]

    def __str__(self):
        return f"{self.plugin}:{self.kind}/{self.namespace}/{self.name}"


class GitOpsSyncRun(models.Model):
    reason = models.CharField(max_length=100, default="manual")
    plugin = models.CharField(max_length=100, blank=True)
    tenant = models.ForeignKey("config.Tenant", null=True, blank=True, on_delete=models.SET_NULL)
    actor = models.ForeignKey("auth.User", null=True, blank=True, on_delete=models.SET_NULL)
    status = models.CharField(
        max_length=20,
        choices=GitOpsSyncRunStatus.choices,
        default=GitOpsSyncRunStatus.RUNNING,
    )
    rendered_count = models.PositiveIntegerField(default=0)
    synced_count = models.PositiveIntegerField(default=0)
    pruned_count = models.PositiveIntegerField(default=0)
    failed_count = models.PositiveIntegerField(default=0)
    error = models.TextField(blank=True)
    started_at = models.DateTimeField(default=timezone.now)
    finished_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-started_at"]
        indexes = [
            models.Index(fields=["status", "started_at"]),
            models.Index(fields=["plugin", "started_at"]),
            models.Index(fields=["tenant", "started_at"]),
        ]

    def __str__(self):
        return f"{self.reason} {self.status} {self.started_at.isoformat()}"


class Tenant(models.Model):
    name = models.CharField(max_length=100)
    grafana_org_id = models.IntegerField(null=True, blank=True)
    gitops_nova_repo = models.CharField(max_length=255, blank=True)
    gitops_nova_repo_url = models.URLField(max_length=500, blank=True)
    gitops_custom_repo = models.CharField(max_length=255, blank=True)
    gitops_custom_repo_url = models.URLField(max_length=500, blank=True)
    random_prefix = models.CharField(max_length=10, default=generate_prefix)
    billing_status = models.CharField(
        max_length=20,
        choices=TenantBillingStatus.choices,
        default=TenantBillingStatus.ACTIVE,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.name

    @property
    def billing_suspended(self):
        return settings.BILLING_ENABLED and self.billing_status == TenantBillingStatus.SUSPENDED

    def save(self, *args, **kwargs):
        # when creating a new tenant create a grafana org
        is_new = self.pk is None
        old_billing_status = None
        if not is_new:
            old_billing_status = (
                Tenant.objects.filter(pk=self.pk).values_list("billing_status", flat=True).first()
            )
        super().save(*args, **kwargs)
        if is_new:
            create_tenant(self.name, self)
        elif settings.BILLING_ENABLED and old_billing_status != self.billing_status:
            tenant_id = self.pk

            def sync_apisix_consumers():
                from config.gitops.sync import sync_gitops_resources

                sync_gitops_resources(
                    plugin_names=["apisix"],
                    tenant_id=tenant_id,
                    reason="tenant_billing_status_changed",
                )

            transaction.on_commit(sync_apisix_consumers, robust=True)


@receiver(pre_delete, sender=Tenant)
def tenant_pre_delete(sender, instance, **kwargs):
    delete_tenant(instance)


@receiver(post_delete, sender=Tenant)
def tenant_post_delete(sender, instance, **kwargs):
    update_grafana_org_mapping()


class TenantRole(models.TextChoices):
    ADMIN = 'admin'  # Grafana Editor
    EDITOR = 'editor'  # Grafana Editor
    VIEWER = 'viewer'  # Grafana Viewer


class TenantUser(models.Model):
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE)
    user = models.ForeignKey('auth.User', on_delete=models.CASCADE)
    role = models.CharField(max_length=20, choices=TenantRole.choices, default=TenantRole.VIEWER)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.user.username} is {self.role} in {self.tenant.name}"

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)

    class Meta:
        # make sure only one user per tenant
        constraints = [
            models.UniqueConstraint(fields=["user", "tenant"], name="uniq_user_single_tenant"),
        ]


@receiver(post_save, sender=TenantUser)
def tenant_user_post_save(sender, instance, created, **kwargs):
    logout_user_from_grafana_using_email(instance.user.email)


@receiver(post_delete, sender=TenantUser)
def tenant_user_post_delete(sender, instance, **kwargs):
    logout_user_from_grafana_using_email(instance.user.email)


class TenantLimit(models.Model):
    """
    Unlimited Users
    Limit Nodes
    Limit Clusters -> Cluster is billed per control plane & vCPU
    """
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE)
    max_nodes = models.IntegerField(default=1)
    max_clusters = models.IntegerField(default=0)
    max_vcpus = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    # billing plan incase tenant has other agreements with me
    billing = models.ForeignKey('TenantBilling', on_delete=models.PROTECT)

    def __str__(self):
        return f"{self.tenant.name} - Limits"


class TenantUsage(models.Model):
    """
    Class that tracks usage of tenant
    """
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE)
    nodes = models.IntegerField(default=0)
    clusters = models.IntegerField(default=0)
    vcpus = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.tenant.name} - Usage"

class TenantBilling(models.Model):
    name = models.CharField(max_length=100)
    price_per_node = models.IntegerField(default=400)
    price_per_cluster = models.IntegerField(default=1500)
    price_per_vcpu = models.IntegerField(default=100)

    def __str__(self):
        return self.name

class Node(models.Model):
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE)
    node_name = models.CharField(max_length=100)  # name that customer can use
    node_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    # uuid that identifies the node also used as username
    enabled = models.BooleanField(default=True)
    # iirc this directly switches the config on and off in NovaCFG
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    billable_from = models.DateTimeField(default=timezone.now)
    billable_until = models.DateTimeField(null=True, blank=True)
    alloy_version = models.CharField(max_length=50, blank=True)
    supervisor_version = models.CharField(max_length=50, blank=True)
    supervisor_arch = models.CharField(max_length=20, blank=True)
    alloy_package_format = models.CharField(max_length=20, blank=True)
    target_supervisor_version = models.ForeignKey(
        SupervisorVersion,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="target_nodes",
    )
    target_alloy_version = models.ForeignKey(
        AlloyVersion,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="target_nodes",
    )
    last_config_poll_at = models.DateTimeField(null=True, blank=True)
    last_seen_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"{self.node_name} - {self.node_id} - {self.tenant.name}"

    def generate_token(self, actor=None):
        if self.tenant.billing_suspended:
            raise ValidationError("Suspended tenants cannot generate installer tokens.")
        now = timezone.now()
        with transaction.atomic():
            active_tokens = list(NodeToken.objects.select_for_update().filter(
                node=self,
                used_at__isnull=True,
                revoked_at__isnull=True,
            ))
            for active_token in active_tokens:
                active_token.revoked_at = now
                active_token.save(update_fields=["revoked_at"])
            token = NodeToken.objects.create(node=self)
        record_audit(
            "node_token.generated",
            "NodeToken",
            actor=actor,
            tenant=self.tenant,
            target_id=token.pk,
            metadata={"node_id": str(self.node_id), "expires_at": token.expires_at.isoformat()},
        )
        return token

    def get_token(self):
        now = timezone.now()
        token = (
            self.tokens.filter(revoked_at__isnull=True, used_at__isnull=True, expires_at__gt=now)
            .order_by("-created_at")
            .first()
        )
        return token or self.generate_token()

    @property
    def last_contact_at(self):
        timestamps = [ts for ts in (self.last_config_poll_at, self.last_seen_at) if ts]
        return max(timestamps) if timestamps else None

    @property
    def effective_last_seen_at(self):
        return self.last_contact_at

    @property
    def status(self):
        if not self.enabled:
            return NodeStatus.DISABLED
        last_seen = self.effective_last_seen_at
        if last_seen is None:
            return NodeStatus.NEVER_CONNECTED
        age = timezone.now() - last_seen
        if age <= timedelta(minutes=5):
            return NodeStatus.ONLINE
        if age <= timedelta(minutes=30):
            return NodeStatus.WARNING
        return NodeStatus.OFFLINE

    @property
    def status_label(self):
        return NodeStatus(self.status).label

    def is_online(self, now=None):
        if not self.enabled or not self.last_contact_at:
            return False
        now = now or timezone.now()
        window = timedelta(seconds=getattr(settings, "NODE_WARNING_WINDOW_SECONDS", 120))
        return self.last_contact_at > now - window

    @property
    def connection_state(self):
        if not self.enabled:
            return "disabled"
        last_contact_at = self.last_contact_at
        if not last_contact_at:
            return "never_connected"
        now = timezone.now()
        warning_window = timedelta(seconds=getattr(settings, "NODE_WARNING_WINDOW_SECONDS", 120))
        failed_window = timedelta(seconds=getattr(settings, "NODE_FAILED_WINDOW_SECONDS", 300))
        if last_contact_at < now - failed_window:
            return "failed"
        if last_contact_at <= now - warning_window:
            return "warning"
        return "online"

    @property
    def connection_status_label(self):
        return {
            "disabled": "Disabled",
            "never_connected": "Never connected",
            "online": "Online",
            "warning": "Warning",
            "failed": "Failed",
        }[self.connection_state]

    @property
    def connection_badge_class(self):
        return {
            "disabled": "badge-ghost",
            "never_connected": "badge-ghost",
            "online": "badge-success",
            "warning": "badge-warning",
            "failed": "badge-error",
        }[self.connection_state]

    def mark_seen(
            self,
            *,
            alloy_version="",
            config_poll=False,
            supervisor_version="",
            supervisor_arch="",
            alloy_package_format="",
    ):
        now = timezone.now()
        updates = {"last_seen_at": now}
        if config_poll:
            updates["last_config_poll_at"] = now
        if alloy_version:
            normalized_alloy_version = normalize_alloy_version(alloy_version)[:50]
            if normalized_alloy_version != self.alloy_version:
                updates["alloy_version"] = normalized_alloy_version
        if supervisor_version:
            normalized_supervisor_version = supervisor_version[:50]
            if normalized_supervisor_version != self.supervisor_version:
                updates["supervisor_version"] = normalized_supervisor_version
        if supervisor_arch:
            normalized_supervisor_arch = supervisor_arch[:20]
            if normalized_supervisor_arch != self.supervisor_arch:
                updates["supervisor_arch"] = normalized_supervisor_arch
        if alloy_package_format:
            normalized_package_format = alloy_package_format[:20]
            if normalized_package_format != self.alloy_package_format:
                updates["alloy_package_format"] = normalized_package_format

        for field, value in updates.items():
            setattr(self, field, value)
        self.save(update_fields=list(updates.keys()))
        self.clear_completed_update_targets()

    def clear_completed_update_targets(self):
        updates = {}
        if (
                self.target_supervisor_version_id
                and self.supervisor_version
                and self.target_supervisor_version.version == self.supervisor_version
        ):
            updates["target_supervisor_version"] = None
        if (
                self.target_alloy_version_id
                and self.alloy_version
                and normalize_alloy_version(self.target_alloy_version.version) == self.alloy_version
        ):
            updates["target_alloy_version"] = None
        if not updates:
            return
        for field in updates:
            setattr(self, field, None)
        self.save(update_fields=list(updates.keys()))

    def record_alloy_version(self, alloy_version):
        if not alloy_version:
            return

        normalized_alloy_version = normalize_alloy_version(alloy_version)[:50]
        if normalized_alloy_version == self.alloy_version:
            return

        self.alloy_version = normalized_alloy_version
        self.save(update_fields=["alloy_version"])
        self.clear_completed_update_targets()

    def record_supervisor_version(self, supervisor_version, supervisor_arch=""):
        updates = {}
        if supervisor_version:
            normalized_supervisor_version = str(supervisor_version).strip()[:50]
            if normalized_supervisor_version != self.supervisor_version:
                updates["supervisor_version"] = normalized_supervisor_version
        if supervisor_arch:
            normalized_supervisor_arch = str(supervisor_arch).strip()[:20]
            if normalized_supervisor_arch != self.supervisor_arch:
                updates["supervisor_arch"] = normalized_supervisor_arch
        if not updates:
            return

        for field, value in updates.items():
            setattr(self, field, value)
        self.save(update_fields=list(updates.keys()))
        self.clear_completed_update_targets()

    def _broadcast_dashboard_status(self):
        from ui.dashboard_live import broadcast_dashboard_status

        broadcast_dashboard_status(self.tenant_id)

    def _push_features_to_redis(self):
        try:
            self.features._push_to_redis()
        except NodeFeatures.DoesNotExist:
            return

    def save(self, *args, **kwargs):
        update_fields = kwargs.get("update_fields")
        if self.alloy_version:
            self.alloy_version = normalize_alloy_version(self.alloy_version)[:50]
        super().save(*args, **kwargs)
        redis_relevant_fields = {"tenant", "tenant_id", "node_name", "node_id", "enabled", "alloy_version"}
        dashboard_relevant_fields = {
            "enabled",
            "last_seen_at",
            "last_config_poll_at",
            "alloy_version",
            "supervisor_version",
            "supervisor_arch",
        }
        should_push_features = update_fields is None or redis_relevant_fields.intersection(update_fields)
        should_broadcast_dashboard = update_fields is None or dashboard_relevant_fields.intersection(update_fields)

        if should_push_features:
            # Only touch Redis after the DB commit.
            # If we edit the node model make sure to pass the update to redis.
            transaction.on_commit(self._push_features_to_redis)
        if should_broadcast_dashboard:
            transaction.on_commit(self._broadcast_dashboard_status)


class NodeFeatures(models.Model):
    node = models.OneToOneField(Node, on_delete=models.CASCADE, related_name='features')
    selfMonitor = models.BooleanField(default=True)
    linuxMonitor = models.BooleanField(default=True)
    windowsMonitor = models.BooleanField(default=False)
    containerMonitor = models.BooleanField(default=False)
    journalLog = models.BooleanField(default=True)
    windowsEventLog = models.BooleanField(default=False)
    dockerLogs = models.BooleanField(default=False)
    fileMonitor = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def _push_to_redis(self):
        from config.file_monitors import active_file_monitor_payload

        file_monitors = active_file_monitor_payload(self.node) if self.fileMonitor and self.node.enabled else []
        observability_tenant_id = tenant_observability_id(self.node.tenant)
        feature_dict = {
            "selfMonitor": self.node.enabled,
            "linuxMonitor": self.linuxMonitor,
            "windowsMonitor": self.windowsMonitor,
            "containerMonitor": self.containerMonitor,
            "journalLog": self.journalLog,
            "windowsEventLog": self.windowsEventLog,
            "dockerLogs": self.dockerLogs,
            "fileMonitor": self.fileMonitor and bool(file_monitors),
            "fileMonitors": file_monitors,
            "alloyVersion": normalize_alloy_version(self.node.alloy_version) or "",
            "Healthcheck_URL": f"{settings.UI_URL.rstrip('/')}/agent/check-in",
            "observabilityTenantID": observability_tenant_id,
        }

        tenant_name = self.node.tenant.name
        node_id = str(self.node.node_id.hex)

        feature_key = f"nova:feat:{tenant_name}:{node_id}"
        credential_key = f"nova:cred:{tenant_name}:{node_id}"
        ver_key = f"nova:ver:{tenant_name}:{node_id}"

        set_json(feature_key, feature_dict, ttl=None)
        set_json(credential_key, self._collector_credentials_payload(), ttl=None)
        set_string(ver_key, str(int(time.time() * 1000)), ttl=None)

    def _collector_credentials_payload(self):
        from agent.models import CollectorAuth

        try:
            auth = self.node.collectorauth
        except CollectorAuth.DoesNotExist:
            return {}
        return {
            "mimir_password": auth.mimir_password,
            "loki_password": auth.loki_password,
            "tempo_password": auth.tempo_password,
        }

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        # Only touch Redis after the DB commit
        transaction.on_commit(self._push_to_redis)

    def __str__(self):
        return f"{self.node.node_name} - Features"

class FileMonitor(models.Model):
    """
    Used to configure file monitor on node
    """
    node = models.ForeignKey(Node, on_delete=models.CASCADE, related_name="file_monitors")
    label = models.CharField(max_length=100, blank=True)
    file_path = models.CharField(max_length=255)
    enabled = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["node", "file_path"], name="uniq_file_monitor_path_per_node"),
        ]
        indexes = [
            models.Index(fields=["node", "enabled"]),
        ]

    def clean(self):
        from config.file_monitors import validate_file_monitor_path

        self.file_path = validate_file_monitor_path(self.file_path)

    def __str__(self):
        return self.label or self.file_path

class NodeToken(models.Model):
    """
    One time token for node to authenticate to NovaCFG/UI
    """
    node = models.ForeignKey('Node', on_delete=models.CASCADE, related_name="tokens")
    token = models.CharField(max_length=255, default=generate_node_token, unique=True)  # auto generated token
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(default=default_node_token_expiry)
    revoked_at = models.DateTimeField(null=True, blank=True)
    used_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["token"]),
            models.Index(fields=["node", "created_at"]),
        ]

    def __str__(self):
        return f"{self.node.node_name} - Token"

    def is_usable(self, now=None):
        now = now or timezone.now()
        return self.used_at is None and self.revoked_at is None and self.expires_at > now

    def validate_usable(self, now=None):
        if self.used_at is not None:
            raise ValidationError("Token has already been used.")
        if self.revoked_at is not None:
            raise ValidationError("Token has been revoked.")
        if self.expires_at <= (now or timezone.now()):
            raise ValidationError("Token has expired.")
        if not self.node.enabled:
            raise ValidationError("Node is disabled.")
        if self.node.tenant.billing_suspended:
            raise ValidationError("Tenant is suspended.")

    def mark_used(self, now=None):
        self.used_at = now or timezone.now()
        self.save(update_fields=["used_at"])


def _safe_related(instance, attr):
    try:
        return getattr(instance, attr)
    except (AttributeError, ObjectDoesNotExist):
        return None


def audit_tenant_for_instance(instance):
    if instance is None:
        return None
    if isinstance(instance, Tenant):
        return instance

    tenant = _safe_related(instance, "tenant")
    if isinstance(tenant, Tenant):
        return tenant

    node = _safe_related(instance, "node")
    if node is not None:
        node_tenant = _safe_related(node, "tenant")
        if isinstance(node_tenant, Tenant):
            return node_tenant

    billing = _safe_related(instance, "billing")
    if billing is not None:
        billing_tenant = _safe_related(billing, "tenant")
        if isinstance(billing_tenant, Tenant):
            return billing_tenant

    return None


def audit_tenant_secure_id(tenant):
    if not tenant:
        return ""
    return f"{tenant.random_prefix}-{tenant.name}"


class TenantAuditLogEntryManager(LogEntryManager):
    def log_create(self, instance, force_log=False, **kwargs):
        tenant = audit_tenant_for_instance(instance)
        if tenant is not None:
            kwargs.setdefault("tenant_pk", tenant.pk)
            kwargs.setdefault("tenant_name", tenant.name)
            kwargs.setdefault("tenant_secure_id", audit_tenant_secure_id(tenant))
            if tenant.pk and Tenant.objects.filter(pk=tenant.pk).exists():
                kwargs.setdefault("tenant", tenant)
        return super().log_create(instance, force_log=force_log, **kwargs)


class TenantAuditLogEntry(AbstractLogEntry):
    tenant = models.ForeignKey(
        "config.Tenant",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
        db_index=True,
    )
    tenant_pk = models.BigIntegerField(null=True, blank=True, db_index=True)
    tenant_name = models.CharField(max_length=100, blank=True, db_index=True)
    tenant_secure_id = models.CharField(max_length=128, blank=True, db_index=True)

    objects = TenantAuditLogEntryManager()

    class Meta(AbstractLogEntry.Meta):
        verbose_name = "tenant audit log entry"
        verbose_name_plural = "tenant audit log entries"
