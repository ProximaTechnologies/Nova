from datetime import date

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Min
from django.utils import timezone

from billing.models import Billing, InvoiceStatus
from config.models import Tenant, TenantBillingStatus


class Command(BaseCommand):
    help = "Update invoice statuses and tenant enforcement based on overdue invoices."

    def handle(self, *args, **options):
        if not settings.BILLING_ENABLED:
            self.stdout.write("Billing is disabled; no invoices or tenants were changed.")
            return
        today = timezone.now().date()
        grace_days = getattr(settings, "BILLING_GRACE_DAYS", 0)
        restricted_days = getattr(settings, "BILLING_RESTRICTED_DAYS", None)
        suspended_days = getattr(settings, "BILLING_SUSPENDED_DAYS", None)

        with transaction.atomic():
            overdue_updated = self._mark_overdue(today)
            enforcement_updated = self._update_enforcement(
                today, grace_days, restricted_days, suspended_days
            )

        self.stdout.write(
            self.style.SUCCESS(
                f"Updated invoices: {overdue_updated}. Updated tenants: {enforcement_updated}."
            )
        )

    def _mark_overdue(self, today: date) -> int:
        overdue_qs = Billing.objects.filter(
            status=InvoiceStatus.AWAITING_PAYMENT,
            due_date__isnull=False,
            due_date__lt=today,
        )
        updated = 0
        for billing in overdue_qs:
            billing.status = InvoiceStatus.OVERDUE
            billing.save(update_fields=["status"])
            updated += 1
        return updated

    def _update_enforcement(
            self,
            today: date,
            grace_days: int,
            restricted_days: int | None,
            suspended_days: int | None,
    ) -> int:
        updated = 0
        overdue_by_tenant = (
            Billing.objects.filter(status=InvoiceStatus.OVERDUE, due_date__isnull=False)
            .values("tenant_id")
            .annotate(oldest_due=Min("due_date"))
        )
        tenant_overdue_map = {
            row["tenant_id"]: (today - row["oldest_due"]).days for row in overdue_by_tenant
        }

        for tenant in Tenant.objects.all():
            overdue_days = tenant_overdue_map.get(tenant.id)
            new_status = self._status_for_overdue_days(
                overdue_days, grace_days, restricted_days, suspended_days
            )
            if tenant.billing_status != new_status:
                tenant.billing_status = new_status
                tenant.save(update_fields=["billing_status"])
                updated += 1

        return updated

    def _status_for_overdue_days(
            self,
            overdue_days: int | None,
            grace_days: int,
            restricted_days: int | None,
            suspended_days: int | None,
    ) -> str:
        if overdue_days is None:
            return TenantBillingStatus.ACTIVE
        if suspended_days is not None and overdue_days >= suspended_days:
            return TenantBillingStatus.SUSPENDED
        if restricted_days is not None and overdue_days >= restricted_days:
            return TenantBillingStatus.RESTRICTED
        if overdue_days >= grace_days:
            return TenantBillingStatus.GRACE
        return TenantBillingStatus.ACTIVE
