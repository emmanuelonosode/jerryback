from django.contrib import admin
from unfold.admin import ModelAdmin as UnfoldModelAdmin, TabularInline as UnfoldTabularInline, StackedInline as UnfoldStackedInline
from django.utils.html import escape, format_html
from django.utils.safestring import mark_safe

from apps.core.money import format_usd
from .views import _DATE_COLUMNS as _DATE_KEYS, _TEXT_COLUMNS as _TEXT_KEYS

from .models import (
    AdverseActionNotice, Client, Lead, LeadActivity, Referrer, ReferralPayout,
    RentalApplication,
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


@admin.register(RentalApplication)
class RentalApplicationAdmin(UnfoldModelAdmin):
    list_display = ("applicant", "status", "property", "move_in_terms", "deadline", "created_at")
    list_filter = ("status",)
    search_fields = ("first_name", "last_name", "email")
    readonly_fields = (
        "verified_at", "decision_due_at", "decided_at", "created_at", "updated_at",
        "move_in_preview", "submitted_application",
    )

    @admin.display(description="What the applicant filled in")
    def submitted_application(self, obj):
        """
        The application, rendered.

        The nested parts — income sources, prior addresses, occupants, pets —
        have no column to live in, so they sit in `draft_data`. Django renders a
        JSONField as a raw textarea, which is unreadable at exactly the moment
        somebody is trying to decide on a person. This lays it out instead.
        """
        data = obj.draft_data or {}
        if not data:
            return "Nothing submitted yet."

        def money(cents):
            try:
                return f"${int(cents) / 100:,.2f}"
            except (TypeError, ValueError):
                return "—"

        blocks = []

        income = data.get("incomeSources") or []
        if income:
            rows = "".join(
                "<li>{} — {}{}</li>".format(
                    escape(str(s.get("kind") or "Income")),
                    escape(money(s.get("monthlyAmountCents"))),
                    " per month" if s.get("monthlyAmountCents") else "",
                )
                for s in income if isinstance(s, dict)
            )
            blocks.append(("Income", f"<ul>{rows}</ul>"))

        prior = data.get("priorAddresses") or []
        if prior:
            rows = "".join(
                "<li>{}{}</li>".format(
                    escape(str(a.get("address") or "—")),
                    " — landlord: " + escape(str(a.get("landlordName")))
                    if a.get("landlordName") else "",
                )
                for a in prior if isinstance(a, dict)
            )
            blocks.append(("Rental history", f"<ul>{rows}</ul>"))

        if data.get("hasPriorEviction") is not None:
            note = data.get("priorEvictionNote") or ""
            blocks.append((
                "Prior eviction",
                escape("Yes" if data["hasPriorEviction"] else "No")
                + (f" — {escape(str(note))}" if note else ""),
            ))

        occupants = data.get("occupants") or []
        if occupants:
            rows = "".join(
                "<li>{}{}</li>".format(
                    escape(str(o.get("name") or "—")),
                    " (minor)" if o.get("isMinor") else "",
                )
                for o in occupants if isinstance(o, dict)
            )
            blocks.append(("Others moving in", f"<ul>{rows}</ul>"))

        pets = data.get("pets") or []
        if pets:
            rows = "".join(
                "<li>{} {}</li>".format(
                    escape(str(p.get("kind") or "Pet")), escape(str(p.get("breed") or "")),
                )
                for p in pets if isinstance(p, dict)
            )
            blocks.append(("Pets", f"<ul>{rows}</ul>"))

        if data.get("disclosuresAcceptedAt"):
            blocks.append(("Disclosures accepted", escape(str(data["disclosuresAcceptedAt"]))))

        # Anything the form gained that this renderer has not caught up with —
        # shown rather than silently dropped, so a new field is visible on day
        # one instead of whenever somebody notices it is missing.
        known = {
            "id", "listingSlug", "attemptedSteps", "updatedAt", "submittedAt",
            "incomeSources", "priorAddresses", "hasPriorEviction", "priorEvictionNote",
            "occupants", "pets", "disclosuresAcceptedAt",
            *_TEXT_KEYS, *_DATE_KEYS,
        }
        extra = {k: v for k, v in data.items() if k not in known and v not in (None, "", [], {})}
        if extra:
            rows = "".join(
                f"<li>{escape(str(k))}: {escape(str(v))}</li>" for k, v in sorted(extra.items())
            )
            blocks.append(("Also submitted", f"<ul>{rows}</ul>"))

        if not blocks:
            return "Only the basic details so far — see the fields above."

        html = "".join(
            f"<p style='margin:10px 0 2px'><strong>{title}</strong></p>{body}"
            for title, body in blocks
        )
        return format_html("<div style='max-width:640px'>{}</div>", mark_safe(html))

    @admin.display(description="Move-in terms")
    def move_in_terms(self, obj):
        """
        Whether this application can be approved yet, shown in the list.

        Approval is refused until the deposit and admin fee are entered, because
        the calculator's fallbacks (one month's rent, the configured default)
        would otherwise become a real invoice nobody chose. Surfacing it here
        means staff see it before opening the record.
        """
        missing = []
        if obj.security_deposit_cents is None:
            missing.append("deposit")
        if obj.lease_admin_fee_cents is None:
            missing.append("admin fee")
        if obj.has_pets and obj.pet_fee_cents is None:
            missing.append("pet fee")
        if missing:
            return format_html('<span style="color:#e0a03a">Set: {}</span>', ", ".join(missing))
        return format_html('<span style="color:#4fc98d">Ready</span>')

    @admin.display(description="What this will invoice at move-in")
    def move_in_preview(self, obj):
        """The exact breakdown approval will charge, before anyone approves."""
        if obj.property_id is None:
            return "No property attached yet."
        if obj.security_deposit_cents is None or obj.lease_admin_fee_cents is None:
            return "Enter the deposit and administration fee to see the breakdown."

        from .move_in import calculate_move_in
        from .services import deposit_ceiling_cents

        breakdown = calculate_move_in(
            monthly_rent_cents=obj.property.price_cents,
            months_upfront=obj.months_rent_upfront,
            security_deposit_cents=obj.security_deposit_cents,
            application_fee_cents=0 if obj.is_fee_paid else (obj.application_fee_cents or 0),
            lease_admin_fee_cents=obj.lease_admin_fee_cents,
            pet_fee_cents=obj.pet_fee_cents if obj.has_pets else 0,
            max_security_deposit_cents=deposit_ceiling_cents(
                obj.property.state, obj.property.price_cents,
            ),
        )
        rows = "".join(
            "<tr><td style='padding:2px 12px 2px 0'>{}</td>"
            "<td style='text-align:right'>${:,.2f}</td></tr>".format(
                item["description"], item["unit_price_cents"] * item.get("quantity", 1) / 100,
            )
            for item in breakdown.line_items
        )
        rows += "<tr><td style='padding-top:6px'><strong>Total</strong></td>" \
                "<td style='text-align:right;padding-top:6px'><strong>${:,.2f}</strong></td></tr>".format(
                    breakdown.total_cents / 100,
                )
        warnings = "".join(
            f"<p style='color:#e0a03a;margin:6px 0 0'>{w}</p>" for w in breakdown.warnings
        )
        return format_html("<table>{}</table>{}", mark_safe(rows), mark_safe(warnings))

    @admin.display(description="Applicant")
    def applicant(self, obj):
        return f"{obj.first_name} {obj.last_name}".strip() or obj.email or str(obj.pk)[:8]

    @admin.display(description="Decision due")
    def deadline(self, obj):
        """
        The 24-hour promise, made visible. It starts at payment verification,
        not submission — with manual rails there is a real gap between someone
        sending money and a person confirming it arrived.
        """
        if not obj.decision_due_at:
            return format_html('<span style="color:#5a6470">clock not started</span>')
        if obj.decided_at:
            return format_html('<span style="color:#0b6b47">decided</span>')
        if obj.is_overdue:
            return format_html('<strong style="color:#b3261e">OVERDUE</strong>')
        return obj.decision_due_at.strftime("%d %b %H:%M")


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

