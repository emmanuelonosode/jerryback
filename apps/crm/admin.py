from django.contrib import admin
from django.utils.html import format_html

from apps.core.money import format_usd
from unfold.admin import ModelAdmin as UnfoldModelAdmin, TabularInline as UnfoldTabularInline

from .models import (
    AdverseActionNotice, Client, DocumentRequest, Lead, LeadActivity, Referrer, ReferralPayout,
)
from .scoring import score_band


class LeadActivityInline(UnfoldTabularInline):
    model = LeadActivity
    extra = 0
    fields = ("activity_type", "note", "agent", "created_at")
    readonly_fields = ("created_at",)


@admin.register(Lead)
class LeadAdmin(UnfoldModelAdmin):
    list_display = ("full_name", "email", "status", "score_display", "has_voucher", "assigned_agent", "created_at")
    list_filter = ("status", "source", "has_voucher", "assigned_agent")
    search_fields = ("full_name", "email", "phone")
    inlines = [LeadActivityInline]
    readonly_fields = ("score_breakdown", "created_at", "updated_at")

    @admin.display(description="Score", ordering=None)
    def score_display(self, obj):
        score = obj.score
        colour = {"hot": "#0b6b47", "warm": "#8a5a0b", "cool": "#5a6470", "cold": "#8892a0"}[score_band(score)]
        return format_html('<strong style="color:{}">{}</strong>', colour, score)

    @admin.display(description="Why this score")
    def score_breakdown(self, obj):
        """
        Shown because an unexplained ranking is one nobody trusts or acts on —
        and because staff need to know the score ranks who to call first and is
        NOT a qualification decision. Screening runs against the published
        criteria, applied consistently.
        """
        rows = "".join(
            f"<li>{r['label']}: <strong>{r['points']:+d}</strong></li>"
            for r in obj.score_detail["reasons"]
        )
        return format_html(
            '<ul style="margin:0;padding-left:1rem">{}</ul>'
            '<p style="margin-top:.5rem;color:#5a6470">Ranks who to call first. '
            'Not a qualification decision.</p>',
            format_html(rows) if rows else "no signals yet",
        )


# The application admin is large enough to live in its own module; importing
# it registers it.
from .application_admin import RentalApplicationAdmin  # noqa: E402,F401


@admin.register(DocumentRequest)
class DocumentRequestAdmin(UnfoldModelAdmin):
    """The queue of documents staff asked for, across every application."""

    list_display = ("application", "kind", "status", "due_at", "created_at")
    list_filter = ("status", "kind")
    search_fields = ("application__first_name", "application__last_name", "application__email")
    autocomplete_fields = ("application",)
    readonly_fields = ("requested_by", "created_at", "updated_at")


@admin.register(AdverseActionNotice)
class AdverseActionNoticeAdmin(UnfoldModelAdmin):
    list_display = ("rental_application", "agency_name", "sent_at", "created_at")
    list_filter = ("sent_at",)
    readonly_fields = ("created_at",)


@admin.register(ReferralPayout)
class ReferralPayoutAdmin(UnfoldModelAdmin):
    list_display = ("referrer", "status", "commission_display", "created_at")
    list_filter = ("status",)
    readonly_fields = ("commission_amount_cents",)

    @admin.display(description="Commission")
    def commission_display(self, obj):
        return format_usd(obj.commission_amount_cents)


@admin.register(Referrer)
class ReferrerAdmin(UnfoldModelAdmin):
    pass

@admin.register(Client)
class ClientAdmin(UnfoldModelAdmin):
    pass

