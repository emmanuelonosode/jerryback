from django.contrib import admin, messages
from unfold.admin import ModelAdmin as UnfoldModelAdmin, TabularInline as UnfoldTabularInline, StackedInline as UnfoldStackedInline
from django.utils.html import format_html

from apps.core.money import format_usd

from .models import Invoice, InvoiceStatus, Payment, PaymentMethodConfig, PaymentStatus
from .emails import send_invoice_email


@admin.register(PaymentMethodConfig)
class PaymentMethodConfigAdmin(UnfoldModelAdmin):
    list_display = ("display_name", "method", "is_active", "irreversible", "has_details")
    list_filter = ("is_active", "irreversible")
    fieldsets = (
        (None, {
            "fields": ("method", "display_name", "is_active", "clearing_time", "irreversible"),
            "description": (
                "These are manual rails: the applicant pays outside the system and a person "
                "confirms it. Details appear ONLY on the site, behind an application the "
                "applicant started — never sent by email or text, because that is exactly what "
                "a scam looks like. A method cannot be activated without a handle or account."
            ),
        }),
        ("Handle", {"fields": ("handle", "extra_instructions")}),
        ("Bank transfer", {
            "fields": ("recipient_name", "bank_name", "account_type", "account_number", "routing_number"),
            "classes": ("collapse",),
        }),
    )

    @admin.display(boolean=True, description="Payable")
    def has_details(self, obj):
        return obj.is_payable


from django.shortcuts import redirect


@admin.register(Invoice)
class InvoiceAdmin(UnfoldModelAdmin):
    list_display = ("invoice_number", "title", "recipient_display", "total_display", "received_display", "status", "due_date", "resend_button")
    list_filter = ("status", "due_date")
    search_fields = ("invoice_number", "title", "user__email", "rental_application__email")
    readonly_fields = ("invoice_number", "email_status_and_resend", "subtotal_cents", "tax_amount_cents", "total_cents", "created_at")
    actions = ["send_invoice_notification"]

    def get_urls(self):
        from django.urls import path
        urls = super().get_urls()
        custom_urls = [
            path(
                "<path:object_id>/resend-email/",
                self.admin_site.admin_view(self.resend_invoice_email_view),
                name="billing_invoice_resend_email",
            ),
        ]
        return custom_urls + urls

    def resend_invoice_email_view(self, request, object_id, *args, **kwargs):
        invoice = self.get_object(request, object_id)
        if not invoice:
            self.message_user(request, "Invoice not found.", level=messages.ERROR)
            return redirect("..")
        sent = send_invoice_email(invoice)
        recipient = (
            invoice.user.email
            if (invoice.user and invoice.user.email)
            else (invoice.rental_application.email if (invoice.rental_application and invoice.rental_application.email) else None)
        )
        if sent:
            if invoice.status == InvoiceStatus.DRAFT:
                invoice.status = InvoiceStatus.SENT
                invoice.save(update_fields=["status", "updated_at"])
            self.message_user(
                request,
                f"Successfully sent/resent Invoice {invoice.invoice_number} email to {recipient or 'resident'} with payment CTA link.",
                level=messages.SUCCESS,
            )
        else:
            self.message_user(
                request,
                f"Failed to send Invoice {invoice.invoice_number} email. Please verify that a User or Rental Application with a valid email address is linked.",
                level=messages.ERROR,
            )
        return redirect(request.META.get("HTTP_REFERER") or "..")

    @admin.display(description="Recipient")
    def recipient_display(self, obj):
        if obj.user and obj.user.email:
            return obj.user.email
        if obj.rental_application and obj.rental_application.email:
            return obj.rental_application.email
        return "No email"

    @admin.display(description="Actions")
    def resend_button(self, obj):
        if not obj.pk:
            return "—"
        from django.urls import reverse
        url = reverse("admin:billing_invoice_resend_email", args=[obj.pk])
        return format_html(
            '<a class="button" href="{}" style="background-color: #059669; color: #ffffff; padding: 4px 10px; border-radius: 4px; font-weight: 600; text-decoration: none; display: inline-block; font-size: 12px; white-space: nowrap;">'
            'Resend Email'
            '</a>',
            url,
        )

    @admin.display(description="Email Delivery & Actions")
    def email_status_and_resend(self, obj):
        if not obj.pk:
            return "Invoice email will be dispatched automatically upon saving."
        from django.urls import reverse
        url = reverse("admin:billing_invoice_resend_email", args=[obj.pk])
        recipient = (
            obj.user.email
            if (obj.user and obj.user.email)
            else (obj.rental_application.email if (obj.rental_application and obj.rental_application.email) else None)
        )
        if not recipient:
            return format_html(
                '<div style="color: #dc2626; font-weight: 600; padding: 4px 0;">'
                'No recipient email linked. Please link a User or Rental Application to deliver invoice.'
                '</div>'
            )
        return format_html(
            '<div style="display: flex; align-items: center; gap: 16px; padding: 6px 0;">'
            '<span>Recipient: <strong style="color: #064e3b;">{}</strong></span>'
            '<a class="button" href="{}" style="background-color: #059669; color: #ffffff; padding: 7px 16px; border-radius: 6px; font-weight: 600; text-decoration: none; display: inline-block; font-size: 13px;">'
            'Resend Invoice Email'
            '</a>'
            '</div>',
            recipient,
            url,
        )

    @admin.display(description="Total")
    def total_display(self, obj):
        return format_usd(obj.total_cents)

    @admin.display(description="Received")
    def received_display(self, obj):
        received = obj.received_cents
        colour = "#0b6b47" if received >= obj.total_cents else "#8a5a0b"
        return format_html('<span style="color:{}">{}</span>', colour, format_usd(received))

    @admin.action(description="Resend Invoice & Payment CTA Email to Resident")
    def send_invoice_notification(self, request, queryset):
        success_count = 0
        fail_count = 0
        for invoice in queryset:
            if send_invoice_email(invoice):
                if invoice.status == InvoiceStatus.DRAFT:
                    invoice.status = InvoiceStatus.SENT
                    invoice.save(update_fields=["status", "updated_at"])
                success_count += 1
            else:
                fail_count += 1

        if success_count:
            self.message_user(request, f"Sent {success_count} invoice email(s) with payment CTA link.", level=messages.SUCCESS)
        if fail_count:
            self.message_user(request, f"Failed to send {fail_count} invoice email(s). Verify recipient email address.", level=messages.WARNING)

    def save_model(self, request, obj, form, change):
        is_new = obj.pk is None
        super().save_model(request, obj, form, change)

        # Dispatch email whenever invoice is created or if marked SENT
        if is_new or obj.status == InvoiceStatus.SENT:
            sent = send_invoice_email(obj)
            recipient = (
                obj.user.email
                if (obj.user and obj.user.email)
                else (obj.rental_application.email if (obj.rental_application and obj.rental_application.email) else "resident")
            )
            if sent:
                if obj.status == InvoiceStatus.DRAFT:
                    obj.status = InvoiceStatus.SENT
                    obj.save(update_fields=["status", "updated_at"])
                self.message_user(
                    request,
                    f"Invoice {obj.invoice_number} saved and emailed successfully to {recipient}.",
                    level=messages.SUCCESS
                )
            else:
                self.message_user(
                    request,
                    f"Invoice {obj.invoice_number} saved, but could not deliver email (verify that a user or application with an email is attached).",
                    level=messages.WARNING
                )


@admin.register(Payment)
class PaymentAdmin(UnfoldModelAdmin):
    # WHOSE payment, and whether there is proof to look at. The list showed a
    # reference and an amount, so finding the application a payment belonged to
    # meant opening it - and the queue is worked by scanning, not by opening.
    list_display = (
        "reference_id", "applicant", "amount_display", "payment_method",
        "status", "has_proof", "verified_by", "created_at",
    )
    list_filter = ("status", "payment_method")
    search_fields = ("reference_id", "rental_application__email",
                     "rental_application__first_name", "rental_application__last_name")
    list_select_related = ("rental_application", "verified_by")
    # Newest first: a queue is worked from what just arrived.
    ordering = ("-created_at",)

    @admin.display(description="Applicant")
    def applicant(self, obj):
        application = obj.rental_application
        if application is None:
            return "—"
        name = f"{application.first_name} {application.last_name}".strip()
        return name or application.email or str(application.id)[:8]

    @admin.display(description="Proof", boolean=True)
    def has_proof(self, obj):
        return bool(obj.proof_image_url)

    @admin.display(description="Proof Image")
    def proof_image_preview(self, obj):
        if obj.proof_image_url:
            from django.urls import reverse
            from pathlib import Path
            safe_name = Path(obj.proof_image_url).name
            view_url = reverse("secure-payment-proof-view", kwargs={"filename": safe_name})
            download_url = f"{view_url}?download=1"
                
            return format_html(
                '<a href="{}" target="_blank" download style="display:inline-block;margin-bottom:10px;text-decoration:underline;color:#0b6b47;">'
                '<strong>Download Proof</strong></a><br/>'
                '<a href="{}" target="_blank">'
                '<img src="{}" style="max-width:400px;border-radius:8px;border:1px solid #ddd" /></a>',
                download_url, view_url, view_url
            )
        return "Not uploaded"

    readonly_fields = ("proof_image_preview", "verified_by", "verified_at", "paid_at", "created_at")
    actions = ["verify_selected"]

    @admin.display(description="Amount")
    def amount_display(self, obj):
        return format_usd(obj.amount_cents)

    @admin.action(description="Verify — confirm the money arrived")
    def verify_selected(self, request, queryset):
        """
        Rejection is deliberately NOT a bulk action.

        Rejecting requires a reason the applicant can be told, and a reason
        typed once for a whole selection is not a reason. It is done one at a
        time, on the record.
        """
        done = 0
        for payment in queryset.exclude(status__in=[PaymentStatus.VERIFIED, PaymentStatus.REJECTED]):
            payment.verify(request.user)
            done += 1
        self.message_user(request, f"{done} payment(s) verified.")
