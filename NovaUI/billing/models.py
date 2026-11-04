from django.conf import settings
from django.db import models
from django.db import transaction
from django.template.loader import render_to_string
from django.utils import timezone


class BillingType(models.TextChoices):
    ONE_TIME = "one_time", "One-time"
    RECURRING = "recurring", "Recurring"


class InvoiceStatus(models.TextChoices):
    DRAFT = "draft", "Draft"
    AWAITING_PAYMENT = "awaiting_payment", "Awaiting payment"
    PAID = "paid", "Paid"
    OVERDUE = "overdue", "Overdue"
    VOID = "void", "Void"


class Billing(models.Model):
    tenant = models.ForeignKey("config.Tenant", on_delete=models.CASCADE, related_name="billings")
    billing_type = models.CharField(
        max_length=20,
        choices=BillingType.choices,
        default=BillingType.ONE_TIME,
    )
    status = models.CharField(
        max_length=20,
        choices=InvoiceStatus.choices,
        default=InvoiceStatus.AWAITING_PAYMENT,
    )
    period_start = models.DateField(null=True, blank=True)
    period_end = models.DateField(null=True, blank=True)
    due_date = models.DateField(null=True, blank=True)
    paid_at = models.DateTimeField(null=True, blank=True)
    nodes = models.IntegerField(default=0)
    clusters = models.IntegerField(default=0)
    vcpus = models.IntegerField(default=0)
    currency = models.CharField(max_length=3, default="USD")
    subtotal = models.IntegerField(default=0)
    total = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.tenant.name} - {self.get_billing_type_display()}"

    def calculate_totals(self):
        from config.models import TenantLimit

        tenant_limit = (
            TenantLimit.objects.filter(tenant=self.tenant).select_related("billing").first()
        )
        if not tenant_limit or not tenant_limit.billing:
            return 0, 0
        plan = tenant_limit.billing
        subtotal = (
                (self.nodes * plan.price_per_node)
                + (self.clusters * plan.price_per_cluster)
                + (self.vcpus * plan.price_per_vcpu)
        )
        return subtotal, subtotal

    def save(self, *args, **kwargs):
        self.subtotal, self.total = self.calculate_totals()
        super().save(*args, **kwargs)

    def render_invoice_html(self):
        return render_to_string("billing/invoice.html", {"billing": self})

    def render_receipt_html(self):
        return render_to_string("billing/receipt.html", {"billing": self})

    def generate_invoice_pdf(self):
        from weasyprint import HTML

        html = self.render_invoice_html()
        htmlfixer = HTML(string=html, base_url=str(settings.BASE_DIR))
        return htmlfixer.write_pdf()

    def generate_receipt_pdf(self):
        from weasyprint import HTML

        html = self.render_receipt_html()
        htmlfixer = HTML(string=html, base_url=str(settings.BASE_DIR))
        return htmlfixer.write_pdf()

    def create_invoice(self):
        if not self.pk:
            self.save()
        with transaction.atomic():
            invoice = BillingInvoice.objects.select_for_update().filter(billing=self).first()
            if not invoice:
                invoice_number, invoice_month, invoice_sequence = self._next_invoice_number()
                invoice = BillingInvoice.objects.create(
                    billing=self,
                    pdf_filename=f"{invoice_number}.pdf",
                    pdf=b"",
                    invoice_number=invoice_number,
                    invoice_month=invoice_month,
                    invoice_sequence=invoice_sequence,
                )
            else:
                if not invoice.invoice_number:
                    invoice.invoice_number, invoice.invoice_month, invoice.invoice_sequence = (
                        self._next_invoice_number()
                    )
                invoice.pdf_filename = f"{invoice.invoice_number}.pdf"
                invoice.save(
                    update_fields=[
                        "pdf_filename",
                        "invoice_number",
                        "invoice_month",
                        "invoice_sequence",
                    ]
                )

        pdf_bytes = self.generate_invoice_pdf()
        invoice.pdf = pdf_bytes
        invoice.save(update_fields=["pdf"])
        return invoice

    def create_receipt(self):
        if not self.pk:
            self.save()
        if self.status != InvoiceStatus.PAID:
            raise ValueError("Receipt can only be generated for paid invoices.")
        pdf_bytes = self.generate_receipt_pdf()
        invoice_number = None
        if hasattr(self, "invoice") and self.invoice:
            invoice_number = self.invoice.invoice_number
        receipt_name = f"Receipt-{invoice_number or self.id}.pdf"
        receipt, _ = BillingReceipt.objects.update_or_create(
            billing=self,
            defaults={
                "pdf_filename": receipt_name,
                "pdf": pdf_bytes,
            },
        )
        return receipt

    def _next_invoice_number(self):
        stamp = timezone.now()
        invoice_month = stamp.strftime("%Y%m")
        with transaction.atomic():
            existing_count = (
                BillingInvoice.objects.select_for_update()
                .filter(invoice_month=invoice_month)
                .count()
            )
            next_seq = existing_count + 1
        invoice_number = f"INV-{invoice_month}{next_seq:03d}"
        return invoice_number, invoice_month, next_seq


class BillingInvoice(models.Model):
    billing = models.OneToOneField(Billing, on_delete=models.CASCADE, related_name="invoice")
    invoice_number = models.CharField(max_length=20, blank=True, null=True)
    invoice_month = models.CharField(max_length=6, blank=True, null=True)
    invoice_sequence = models.IntegerField(blank=True, null=True)
    pdf_filename = models.CharField(max_length=255)
    pdf = models.BinaryField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Invoice {self.pdf_filename}"


class BillingReceipt(models.Model):
    billing = models.OneToOneField(Billing, on_delete=models.CASCADE, related_name="receipt")
    pdf_filename = models.CharField(max_length=255)
    pdf = models.BinaryField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Receipt {self.pdf_filename}"
