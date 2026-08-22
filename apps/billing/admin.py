from django.contrib import admin
from unfold.admin import ModelAdmin as UnfoldModelAdmin, TabularInline as UnfoldTabularInline, StackedInline as UnfoldStackedInline
from django.utils.html import format_html

from apps.core.money import format_usd

from .models import Invoice, Payment, PaymentMethodConfig, PaymentStatus


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


@admin.register(Invoice)
class InvoiceAdmin(UnfoldModelAdmin):
    list_display = ("invoice_number", "title", "total_display", "received_display", "status", "due_date")
    list_filter = ("status", "due_date")
    search_fields = ("invoice_number", "title")
    readonly_fields = ("invoice_number", "subtotal_cents", "tax_amount_cents", "total_cents", "created_at")

    @admin.display(description="Total")
    def total_display(self, obj):
        return format_usd(obj.total_cents)

    @admin.display(description="Received")
    def received_display(self, obj):
        received = obj.received_cents
        colour = "#0b6b47" if received >= obj.total_cents else "#8a5a0b"
        return format_html('<span style="color:{}">{}</span>', colour, format_usd(received))


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
    readonly_fields = ("verified_by", "verified_at", "paid_at", "created_at")
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
