import os
import secrets
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.core.validators import validate_email
from django.db import transaction

from config.models import (
    SupervisorVersion,
    SupervisorVersionArtifact,
    Tenant,
    TenantBilling,
    TenantLimit,
    TenantRole,
    TenantUsage,
    TenantUser,
    is_supervisor_semver,
    record_audit,
)
from config.oidc_bootstrap import bootstrap_grafana_oidc_client

DEFAULT_TENANT_NAME = "Nova"
DEFAULT_BILLING_PLAN_NAME = "Default"
DEBUG_BOOTSTRAP_EMAIL = "admin@domain.com"
DEBUG_BOOTSTRAP_PASSWORD = "P@ssw0rd"
SUPPORTED_SUPERVISOR_ARCHES = ("amd64", "arm64", "ppc64le", "s390x")


class Command(BaseCommand):
    help = "Bootstrap a first-install NovaUI owner, tenant, billing plan, and Grafana OIDC client."

    def add_arguments(self, parser):
        parser.add_argument(
            "--email",
            default=os.getenv("NOVA_BOOTSTRAP_EMAIL"),
            help="Owner email address. Defaults to NOVA_BOOTSTRAP_EMAIL.",
        )
        parser.add_argument(
            "--username",
            default=os.getenv("NOVA_BOOTSTRAP_USERNAME"),
            help="Owner username. Defaults to NOVA_BOOTSTRAP_USERNAME or the email local part.",
        )
        parser.add_argument(
            "--password",
            default=os.getenv("NOVA_BOOTSTRAP_PASSWORD"),
            help="Owner password. Defaults to a generated password for new users, or P@ssw0rd in DEBUG.",
        )
        parser.add_argument(
            "--tenant-name",
            default=os.getenv("NOVA_BOOTSTRAP_TENANT", DEFAULT_TENANT_NAME),
            help=f"Initial tenant name. Defaults to NOVA_BOOTSTRAP_TENANT or {DEFAULT_TENANT_NAME!r}.",
        )
        parser.add_argument(
            "--billing-plan-name",
            default=DEFAULT_BILLING_PLAN_NAME,
            help=f"Billing plan required by tenant provisioning. Defaults to {DEFAULT_BILLING_PLAN_NAME!r}.",
        )
        parser.add_argument(
            "--allow-existing-tenants",
            action="store_true",
            help="Allow creating the named tenant even when other tenants already exist.",
        )
        parser.add_argument(
            "--no-superuser",
            action="store_true",
            help="Do not promote the owner account to Django staff/superuser.",
        )
        parser.add_argument(
            "--noinput",
            "--no-input",
            action="store_false",
            dest="interactive",
            help="Do not prompt for missing values.",
        )
        parser.add_argument(
            "--client-name",
            help="OIDC client name to create or update. Defaults to GRAFANA_OAUTH_CLIENT_NAME.",
        )
        parser.add_argument(
            "--client-id",
            help="Client ID to use for a new or existing Grafana OIDC client.",
        )
        parser.add_argument(
            "--client-secret",
            help="Client secret to use for a new or existing Grafana OIDC client.",
        )
        parser.add_argument(
            "--redirect-uri",
            help="Grafana OAuth callback URI. Defaults to <GRAFANA_EXTERNAL_URL>/login/generic_oauth.",
        )
        parser.add_argument(
            "--rotate-client-secret",
            action="store_true",
            help="Generate a new client secret for an existing Grafana OIDC client.",
        )
        parser.add_argument(
            "--skip-grafana-oidc",
            action="store_true",
            help="Skip Grafana OIDC client bootstrap. Tenant creation may still require existing OIDC credentials.",
        )
        parser.add_argument(
            "--sync-grafana",
            action="store_true",
            help="Push OIDC client credentials and org mappings to Grafana after bootstrap.",
        )
        parser.add_argument(
            "--register-supervisor-version",
            action="store_true",
            help="Register embedded Nova Supervisor binary artifacts from SHA256SUMS.",
        )
        parser.add_argument(
            "--supervisor-version",
            default=os.getenv("SUPERVISOR_VERSION") or os.getenv("NOVA_SUPERVISOR_VERSION"),
            help="Supervisor version to register. Defaults to SUPERVISOR_VERSION or NOVA_SUPERVISOR_VERSION.",
        )
        parser.add_argument(
            "--supervisor-binary-dir",
            default=os.getenv("SUPERVISOR_BINARY_DIR") or os.getenv("NOVA_SUPERVISOR_BINARY_DIR"),
            help="Directory containing supervisor binaries and SHA256SUMS.",
        )

    def handle(self, *args, **options):
        email = self._resolve_email(options)
        username = self._resolve_username(email, options["username"])
        password, password_source = self._resolve_password(email, username, options["password"])
        tenant_name = options["tenant_name"].strip()
        billing_plan_name = options["billing_plan_name"].strip()
        validate_owner_password = password_source != "debug_default"

        if not tenant_name:
            raise CommandError("Tenant name cannot be empty.")
        if not billing_plan_name:
            raise CommandError("Billing plan name cannot be empty.")
        self._validate_owner_precondition(
            email,
            username,
            password,
            validate_owner_password=validate_owner_password,
        )
        self._validate_tenant_precondition(
            tenant_name=tenant_name,
            allow_existing_tenants=options["allow_existing_tenants"],
        )
        supervisor_registration = None
        if options["register_supervisor_version"]:
            supervisor_registration = self._load_supervisor_registration(
                version=options["supervisor_version"] or settings.NOVA_SUPERVISOR_VERSION,
                binary_dir=options["supervisor_binary_dir"] or settings.NOVA_SUPERVISOR_BINARY_DIR,
            )

        try:
            oidc_result = None
            if not options["skip_grafana_oidc"]:
                oidc_result = bootstrap_grafana_oidc_client(
                    client_name=options["client_name"],
                    client_id=options["client_id"],
                    client_secret=options["client_secret"],
                    redirect_uri=options["redirect_uri"],
                    rotate_client_secret=options["rotate_client_secret"],
                )

            with transaction.atomic():
                billing_plan, billing_created = TenantBilling.objects.get_or_create(name=billing_plan_name)
                user, user_created, password_changed = self._ensure_owner_user(
                    email=email,
                    username=username,
                    password=password,
                    validate_owner_password=validate_owner_password,
                    promote_to_superuser=not options["no_superuser"],
                )
                self._ensure_verified_email(user, email)
                tenant, tenant_created = self._ensure_tenant(
                    tenant_name=tenant_name,
                    allow_existing_tenants=options["allow_existing_tenants"],
                )
                self._ensure_tenant_usage_and_limits(tenant, billing_plan)
                membership, membership_created, membership_role_changed = self._ensure_admin_membership(user, tenant)
                if supervisor_registration and supervisor_registration["artifacts"]:
                    supervisor_registration["artifact_count"] = self._ensure_supervisor_registration(
                        version=supervisor_registration["version"],
                        artifacts=supervisor_registration["artifacts"],
                    )

                record_audit(
                    "install.bootstrapped",
                    "Tenant",
                    actor=user,
                    tenant=tenant,
                    target_id=tenant.pk,
                    metadata={
                        "user_created": user_created,
                        "tenant_created": tenant_created,
                        "membership_created": membership_created,
                    },
                )

            if options["sync_grafana"]:
                from config.integration import update_grafana_org_mapping

                update_grafana_org_mapping()
        except Exception as exc:
            if isinstance(exc, CommandError):
                raise
            raise CommandError(str(exc)) from exc

        self._write_success(
            user=user,
            user_created=user_created,
            password_changed=password_changed,
            tenant=tenant,
            tenant_created=tenant_created,
            billing_plan=billing_plan,
            billing_created=billing_created,
            membership=membership,
            membership_created=membership_created,
            membership_role_changed=membership_role_changed,
            oidc_result=oidc_result,
            synced_grafana=options["sync_grafana"],
            supervisor_registration=supervisor_registration,
            generated_password=password if password_source in ("generated", "debug_default") else "",
        )

    def _resolve_email(self, options):
        email = (options["email"] or "").strip()
        if not email and settings.DEBUG:
            email = DEBUG_BOOTSTRAP_EMAIL
        if not email and options["interactive"]:
            email = input("Owner email: ").strip()
        if not email:
            raise CommandError("Owner email is required. Pass --email or set NOVA_BOOTSTRAP_EMAIL.")
        try:
            validate_email(email)
        except ValidationError as exc:
            raise CommandError(f"Invalid owner email: {email}") from exc
        return email

    def _resolve_username(self, email, username):
        username = (username or "").strip()
        if username:
            return username
        return email.split("@", 1)[0]

    def _resolve_password(self, email, username, password):
        if password:
            return password, "supplied"

        UserModel = get_user_model()
        owner_exists = UserModel._default_manager.filter(email__iexact=email).exists()
        owner_exists = owner_exists or UserModel._default_manager.filter(username=username).exists()
        if owner_exists:
            return None, "unchanged"

        if settings.DEBUG:
            return DEBUG_BOOTSTRAP_PASSWORD, "debug_default"

        user = UserModel(username=username, email=email)
        return self._generate_owner_password(user), "generated"

    def _generate_owner_password(self, user):
        for _ in range(10):
            password = secrets.token_urlsafe(24)
            try:
                self._validate_owner_password(password, user)
            except CommandError:
                continue
            return password
        raise CommandError("Could not generate a valid owner password.")

    def _validate_owner_password(self, password, user, *, use_configured_validators=True):
        try:
            validators = None if use_configured_validators else []
            validate_password(password, user=user, password_validators=validators)
        except ValidationError as exc:
            raise CommandError("; ".join(exc.messages)) from exc

    def _validate_owner_precondition(self, email, username, password, *, validate_owner_password):
        UserModel = get_user_model()
        email_user = UserModel._default_manager.filter(email__iexact=email).order_by("pk").first()
        username_user = UserModel._default_manager.filter(username=username).order_by("pk").first()
        if email_user and username_user and email_user.pk != username_user.pk:
            raise CommandError("--email and --username resolve to different existing users.")

        owner_user = email_user or username_user
        if not password and owner_user is None:
            raise CommandError("Owner password is required for a new user.")
        if password:
            validation_user = owner_user or UserModel(username=username, email=email)
            validation_user.email = email
            validation_user.username = username
            self._validate_owner_password(
                password,
                validation_user,
                use_configured_validators=validate_owner_password,
            )

        from allauth.account.models import EmailAddress

        attached_email = EmailAddress.objects.filter(email__iexact=email)
        if owner_user is not None:
            attached_email = attached_email.exclude(user=owner_user)
        if attached_email.exists():
            raise CommandError("Owner email is already attached to another user.")

    def _validate_tenant_precondition(self, *, tenant_name, allow_existing_tenants):
        if Tenant.objects.filter(name=tenant_name).exists():
            return
        if Tenant.objects.exists() and not allow_existing_tenants:
            raise CommandError(
                "This install already has tenants. Pass --allow-existing-tenants to create another tenant."
            )

    def _ensure_owner_user(self, *, email, username, password, validate_owner_password, promote_to_superuser):
        UserModel = get_user_model()
        email_user = UserModel._default_manager.filter(email__iexact=email).order_by("pk").first()
        username_user = UserModel._default_manager.filter(username=username).order_by("pk").first()
        if email_user and username_user and email_user.pk != username_user.pk:
            raise CommandError("--email and --username resolve to different existing users.")

        user = email_user or username_user
        user_created = user is None
        password_changed = False

        if user is None:
            if not password:
                raise CommandError("Owner password is required for a new user.")
            user = UserModel(username=username, email=email)
            self._validate_owner_password(
                password,
                user,
                use_configured_validators=validate_owner_password,
            )
            user.set_password(password)
            password_changed = True
        else:
            if user.email != email:
                user.email = email
            if user.username != username and username_user is None:
                user.username = username
            if password:
                self._validate_owner_password(
                    password,
                    user,
                    use_configured_validators=validate_owner_password,
                )
                user.set_password(password)
                password_changed = True

        if promote_to_superuser:
            user.is_staff = True
            user.is_superuser = True

        user.full_clean(exclude=["password"])
        user.save()
        return user, user_created, password_changed

    def _ensure_verified_email(self, user, email):
        from allauth.account.models import EmailAddress

        if EmailAddress.objects.filter(email__iexact=email).exclude(user=user).exists():
            raise CommandError("Owner email is already attached to another user.")

        EmailAddress.objects.filter(user=user).exclude(email__iexact=email).update(primary=False)
        email_address = EmailAddress.objects.filter(user=user, email__iexact=email).order_by("pk").first()
        if email_address is None:
            EmailAddress.objects.create(user=user, email=email, verified=True, primary=True)
            return
        email_address.email = email
        email_address.verified = True
        email_address.primary = True
        email_address.save(update_fields=["email", "verified", "primary"])

    def _ensure_tenant(self, *, tenant_name, allow_existing_tenants):
        tenant = Tenant.objects.filter(name=tenant_name).order_by("pk").first()
        if tenant is not None:
            return tenant, False
        return Tenant.objects.create(name=tenant_name), True

    def _ensure_tenant_usage_and_limits(self, tenant, billing_plan):
        TenantUsage.objects.get_or_create(tenant=tenant)
        TenantLimit.objects.get_or_create(tenant=tenant, defaults={"billing": billing_plan})

    def _ensure_admin_membership(self, user, tenant):
        membership, created = TenantUser.objects.get_or_create(
            user=user,
            tenant=tenant,
            defaults={"role": TenantRole.ADMIN},
        )
        role_changed = False
        if membership.role != TenantRole.ADMIN:
            membership.role = TenantRole.ADMIN
            membership.save(update_fields=["role", "updated_at"])
            role_changed = True
        return membership, created, role_changed

    def _load_supervisor_registration(self, *, version, binary_dir):
        version = str(version or "").strip()
        if not is_supervisor_semver(version):
            reason = f"version {version or '<empty>'!r} is not SemVer"
            self.stderr.write(self.style.WARNING(f"Skipping supervisor registration: {reason}."))
            return {"version": version, "artifacts": [], "skipped": reason}

        binary_dir = Path(binary_dir or settings.NOVA_SUPERVISOR_BINARY_DIR)
        sums_path = binary_dir / "SHA256SUMS"
        if not sums_path.is_file():
            raise CommandError(f"Supervisor SHA256SUMS not found: {sums_path}")

        artifacts = {}
        expected_names = {
            Path(settings.NOVA_SUPERVISOR_BINARY_NAME_TEMPLATE.format(version=version, arch=arch)).name: arch
            for arch in SUPPORTED_SUPERVISOR_ARCHES
        }
        for line in sums_path.read_text(encoding="utf-8").splitlines():
            parts = line.split()
            if len(parts) < 2:
                continue
            sha256, filename = parts[0].lower(), Path(parts[1]).name
            arch = expected_names.get(filename)
            if arch is None:
                continue
            if not self._is_sha256(sha256):
                raise CommandError(f"Invalid SHA-256 for supervisor artifact {filename}: {sha256}")
            artifact_path = binary_dir / filename
            if not artifact_path.is_file():
                raise CommandError(f"Supervisor artifact listed in SHA256SUMS is missing: {artifact_path}")
            artifacts[arch] = sha256

        if not artifacts:
            raise CommandError(f"No supervisor artifacts for version {version} found in {sums_path}")
        return {
            "version": version,
            "artifacts": [
                {"arch": arch, "sha256": artifacts[arch]}
                for arch in SUPPORTED_SUPERVISOR_ARCHES
                if arch in artifacts
            ],
            "skipped": "",
        }

    def _ensure_supervisor_registration(self, *, version, artifacts):
        release, _ = SupervisorVersion.objects.update_or_create(version=version, defaults={"enabled": True})
        for artifact in artifacts:
            SupervisorVersionArtifact.objects.update_or_create(
                version=release,
                arch=artifact["arch"],
                defaults={"sha256": artifact["sha256"], "enabled": True},
            )
        return release.artifacts.count()

    def _is_sha256(self, value):
        return len(value) == 64 and all(char in "0123456789abcdef" for char in value)

    def _write_success(
            self,
            *,
            user,
            user_created,
            password_changed,
            tenant,
            tenant_created,
            billing_plan,
            billing_created,
            membership,
            membership_created,
            membership_role_changed,
            oidc_result,
            synced_grafana,
            supervisor_registration,
            generated_password,
    ):
        oidc_message = "skipped"
        if oidc_result is not None:
            client_action = "created" if oidc_result.client_created else "updated"
            key_action = "created" if oidc_result.rsa_key_created else "existing"
            oidc_message = (
                f"{client_action} client_id={oidc_result.client.client_id} "
                f"redirect_uri={oidc_result.redirect_uri} rsa_key={key_action}"
            )
        supervisor_message = "skipped"
        if supervisor_registration is not None:
            if supervisor_registration["skipped"]:
                supervisor_message = f"skipped:{supervisor_registration['skipped']}"
            else:
                supervisor_message = (
                    f"{supervisor_registration['version']}:"
                    f"{supervisor_registration.get('artifact_count', 0)} artifact(s)"
                )

        if generated_password:
            self.stdout.write(f"Owner password: {generated_password}")

        self.stdout.write(
            self.style.SUCCESS(
                "NovaUI install bootstrap completed: "
                f"user={'created' if user_created else 'updated'}:{user.username} "
                f"password={'set' if password_changed else 'unchanged'} "
                f"tenant={'created' if tenant_created else 'existing'}:{tenant.name} "
                f"tenant_id_secure={tenant.random_prefix}-{tenant.name} "
                f"billing_plan={'created' if billing_created else 'existing'}:{billing_plan.name} "
                f"membership={'created' if membership_created else 'existing'}:{membership.role} "
                f"membership_role={'changed' if membership_role_changed else 'unchanged'} "
                f"grafana_oidc={oidc_message} "
                f"grafana_sync={'yes' if synced_grafana else 'no'} "
                f"supervisor_registration={supervisor_message}"
            )
        )
