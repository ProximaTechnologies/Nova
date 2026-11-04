from django.conf import settings
from django.contrib import admin, messages
from django.core.exceptions import PermissionDenied
from django.urls import path, reverse
from django.utils import timezone
from django.utils.html import format_html
from django.http import HttpResponse
from django.shortcuts import get_object_or_404

from .models import Billing, BillingInvoice, BillingReceipt, InvoiceStatus

UNCHANGED = object()


class OptionalBillingAdmin(admin.ModelAdmin):
    def has_module_permission(self, request):
        return settings.BILLING_ENABLED and super().has_module_permission(request)

    def has_view_permission(self, request, obj=None):
        return settings.BILLING_ENABLED and super().has_view_permission(request, obj)

    def has_add_permission(self, request):
        return settings.BILLING_ENABLED and super().has_add_permission(request)

    def has_change_permission(self, request, obj=None):
        return settings.BILLING_ENABLED and super().has_change_permission(request, obj)

    def has_delete_permission(self, request, obj=None):
        return settings.BILLING_ENABLED and super().has_delete_permission(request, obj)


@admin.register(Billing)
class BillingAdmin(OptionalBillingAdmin):
    list_display = (
        "tenant",
        "billing_type",
        "status",
        "due_date",
        "paid_at",
        "nodes",
        "clusters",
        "vcpus",
        "total",
        "invoice_link",
        "receipt_link",
        "created_at",
    )
    list_filter = ("billing_type", "status", "created_at")
    search_fields = ("tenant__name",)
    actions = (
        "mark_draft",
        "mark_awaiting_payment",
        "mark_paid",
        "mark_overdue",
        "mark_void",
        "generate_invoice_pdf",
        "generate_receipt_pdf",
    )

    def invoice_link(self, obj):
        if not hasattr(obj, "invoice"):
            return "—"
        url = reverse("admin:billing_invoice_pdf", args=[obj.invoice.id])
        return format_html('<a href="{}">Download PDF</a>', url)

    invoice_link.short_description = "Invoice PDF"

    def receipt_link(self, obj):
        if obj.status != InvoiceStatus.PAID:
            return "—"
        if not hasattr(obj, "receipt"):
            return "—"
        url = reverse("admin:billing_receipt_pdf", args=[obj.receipt.id])
        return format_html('<a href="{}">Download PDF</a>', url)

    receipt_link.short_description = "Receipt PDF"

    def _set_status(self, queryset, *, status, paid_at=UNCHANGED):
        updated = 0
        update_fields = ["status"]
        if paid_at is not UNCHANGED:
            update_fields.append("paid_at")
        for billing in queryset:
            billing.status = status
            if paid_at is not UNCHANGED:
                billing.paid_at = paid_at
            billing.save(update_fields=update_fields)
            updated += 1
        return updated

    def mark_draft(self, request, queryset):
        updated = self._set_status(queryset, status=InvoiceStatus.DRAFT, paid_at=None)
        self.message_user(request, f"Updated {updated} invoice(s) to DRAFT.", messages.SUCCESS)

    def mark_awaiting_payment(self, request, queryset):
        updated = self._set_status(queryset, status=InvoiceStatus.AWAITING_PAYMENT, paid_at=None)
        self.message_user(
            request, f"Updated {updated} invoice(s) to AWAITING_PAYMENT.", messages.SUCCESS
        )

    def mark_paid(self, request, queryset):
        updated = self._set_status(queryset, status=InvoiceStatus.PAID, paid_at=timezone.now())
        self.message_user(request, f"Updated {updated} invoice(s) to PAID.", messages.SUCCESS)

    def mark_overdue(self, request, queryset):
        updated = self._set_status(queryset, status=InvoiceStatus.OVERDUE)
        self.message_user(request, f"Updated {updated} invoice(s) to OVERDUE.", messages.SUCCESS)

    def mark_void(self, request, queryset):
        updated = self._set_status(queryset, status=InvoiceStatus.VOID, paid_at=None)
        self.message_user(request, f"Updated {updated} invoice(s) to VOID.", messages.SUCCESS)

    def generate_invoice_pdf(self, request, queryset):
        generated = 0
        for billing in queryset:
            billing.create_invoice()
            generated += 1
        self.message_user(request, f"Generated {generated} invoice PDF(s).", messages.SUCCESS)

    def generate_receipt_pdf(self, request, queryset):
        generated = 0
        skipped = 0
        for billing in queryset:
            if billing.status != InvoiceStatus.PAID:
                skipped += 1
                continue
            billing.create_receipt()
            generated += 1
        if generated:
            self.message_user(request, f"Generated {generated} receipt PDF(s).", messages.SUCCESS)
        if skipped:
            self.message_user(
                request,
                f"Skipped {skipped} invoice(s) not marked PAID.",
                messages.WARNING,
            )


@admin.register(BillingInvoice)
class BillingInvoiceAdmin(OptionalBillingAdmin):
    list_display = ("billing", "invoice_number", "pdf_filename", "download_link", "created_at")
    search_fields = ("invoice_number", "pdf_filename", "billing__tenant__name")
    readonly_fields = ("created_at", "updated_at")

    def get_urls(self):
        urls = super().get_urls()
        custom = [
            path(
                "<int:invoice_id>/pdf/",
                self.admin_site.admin_view(self.invoice_pdf_view),
                name="billing_invoice_pdf",
            ),
        ]
        return custom + urls

    def download_link(self, obj):
        url = reverse("admin:billing_invoice_pdf", args=[obj.id])
        return format_html('<a href="{}">Download PDF</a>', url)

    download_link.short_description = "PDF"

    def invoice_pdf_view(self, request, invoice_id):
        invoice = get_object_or_404(BillingInvoice, id=invoice_id)
        if not self.has_view_permission(request, invoice):
            raise PermissionDenied
        response = HttpResponse(invoice.pdf, content_type="application/pdf")
        response["Content-Disposition"] = f'attachment; filename="{invoice.pdf_filename}"'
        return response


@admin.register(BillingReceipt)
class BillingReceiptAdmin(OptionalBillingAdmin):
    list_display = ("billing", "pdf_filename", "download_link", "created_at")
    search_fields = ("pdf_filename", "billing__tenant__name")
    readonly_fields = ("created_at", "updated_at")

    def get_urls(self):
        urls = super().get_urls()
        custom = [
            path(
                "<int:receipt_id>/pdf/",
                self.admin_site.admin_view(self.receipt_pdf_view),
                name="billing_receipt_pdf",
            ),
        ]
        return custom + urls

    def download_link(self, obj):
        url = reverse("admin:billing_receipt_pdf", args=[obj.id])
        return format_html('<a href="{}">Download PDF</a>', url)

    download_link.short_description = "PDF"

    def receipt_pdf_view(self, request, receipt_id):
        receipt = get_object_or_404(BillingReceipt, id=receipt_id)
        if not self.has_view_permission(request, receipt):
            raise PermissionDenied
        response = HttpResponse(receipt.pdf, content_type="application/pdf")
        response["Content-Disposition"] = f'attachment; filename="{receipt.pdf_filename}"'
        return response
