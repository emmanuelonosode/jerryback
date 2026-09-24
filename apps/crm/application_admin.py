"""
The rental application, as staff read it.

WHY THIS WAS REBUILT. Staff opened an application and saw a name, an email and
a list of raw `key: value` lines. Two causes: the form and the column sync used
different names for the same answers (fixed in views.py), and the admin's
fieldsets left out some forty columns, which Django then does not render at
all - date of birth, the background answers, income, household, payment proof.

Now it is tabs, one per thing a person deciding on an application needs:
who they are, what they declared, what they earn, who is moving in, what they
paid, what documents they sent, and the lease. Every answer is shown in words,
money as dollars, and nothing in the draft is dropped - anything not laid out
elsewhere appears on the last tab.

PII IS GATED. The full SSN, licence number and date of birth need the
`application:read-pii` grant (apps/accounts/permissions.py). Deciding needs
income and history, not a date of birth; an agent sees the last four digits.
"""

from pathlib import Path

from django.conf import settings
from django.contrib import admin, messages
from django.urls import reverse
from django.utils import timezone
from django.utils.html import escape, format_html, format_html_join
from django.utils.safestring import mark_safe
from unfold.admin import ModelAdmin as UnfoldModelAdmin
from unfold.admin import StackedInline as UnfoldStackedInline
from unfold.admin import TabularInline as UnfoldTabularInline

from apps.accounts.permissions import APPLICATION_READ_PII, can
from apps.core.money import format_usd
from apps.integrations.models import queue_email

from .documents import email_rejection, email_request
from .lease import build_lease_terms
from .models import (
    ApplicationDocument, ApplicationStatus, DocumentRequest, DocumentRequestStatus, Guarantor,
    RentalApplication,
)

# --- rendering helpers --------------------------------------------------------

_MUTED = "color:#8892a0"


def _yes_no(value) -> str:
    if value is True:
        return "Yes"
    if value is False:
        return "No"
    return "Not answered"


def _money(cents) -> str:
    try:
        return format_usd(int(cents))
    except (TypeError, ValueError):
        return "—"


def _table(rows) -> str:
    """rows: [(label, value)] -> a two-column table. Values are escaped."""
    body = format_html_join(
        "",
        "<tr><th style='text-align:left;padding:4px 16px 4px 0;font-weight:600;vertical-align:top'>{}</th>"
        "<td style='padding:4px 0'>{}</td></tr>",
        ((label, value if value not in (None, "") else "—") for label, value in rows),
    )
    return format_html("<div class='srg-scroll'><table class='srg-kv' style='border-collapse:collapse'>{}</table></div>", body)


def _list_table(headers, rows) -> str:
    if not rows:
        return format_html("<span style='{}'>None</span>", _MUTED)
    head = format_html_join("", "<th style='text-align:left;padding:4px 12px 4px 0'>{}</th>", ((h,) for h in headers))
    body = format_html_join(
        "", "<tr>{}</tr>",
        ((format_html_join("", "<td style='padding:4px 12px 4px 0;vertical-align:top'>{}</td>",
                           ((c if c not in (None, "") else "—",) for c in row)),) for row in rows),
    )
    # Wide tables scroll inside their own box on a phone rather than
    # stretching the whole admin page sideways.
    return format_html("<div class='srg-scroll'><table style='border-collapse:collapse;min-width:100%'><thead><tr>{}</tr></thead><tbody>{}</tbody></table></div>", head, body)


def _section(title, content) -> str:
    return format_html("<h3 style='margin:18px 0 6px;font-weight:600'>{}</h3>{}", title, content)


# The form's income source codes, in words (frontend lib/apply/draft.ts).
_SOURCE_LABELS = {
    "job": "Job", "self-employment": "Self-employment", "benefits": "Benefits",
    "voucher": "Housing voucher", "support": "Child/spousal support", "other": "Other",
}

# Keys laid out on a named tab. Anything else in the draft goes on the last tab.
_SHOWN_KEYS = {
    "id", "listingSlug", "attemptedSteps", "furthestStep", "updatedAt", "submittedAt",
    "firstName", "middleName", "lastName", "email", "phone", "phoneType", "preferredContactMethod",
    "emergencyContactName", "emergencyContactRelationship", "emergencyContactPhone", "emergencyContactPhoneType",
    "preferredMoveInDate", "moveInDate", "dateOfBirth", "idType", "hasLicense", "driversLicenseState",
    "hasEviction", "hasFelony", "hasBankruptcy", "backgroundExplanation", "isActiveMilitary",
    "receivesHousingAssistance", "householdMonthlyIncomeCents", "grossMonthlyCents", "grossAnnualCents",
    "incomeSources", "incomeSource", "employerName", "durationMonths", "guarantor",
    "adultCount", "hasMinorsOrDependents", "dependentCount", "hasMotorVehicles", "vehicles",
    "hasAnimals", "pets", "paymentMethod", "paymentReportedAt", "applicationFeeCents",
    "paymentReference", "paymentProofPath", "paymentProofRejected", "paymentVerifiedAt",
}


def _humanise(key: str) -> str:
    out = "".join(f" {c.lower()}" if c.isupper() else c for c in key)
    return out[:1].upper() + out[1:]


def _humanise_value(key: str, value) -> str:
    if isinstance(value, bool):
        return _yes_no(value)
    if key.endswith("Cents") and isinstance(value, int):
        return _money(value)
    if isinstance(value, list):
        return "; ".join(
            ", ".join(f"{_humanise(k)}: {v}" for k, v in item.items() if v not in (None, "")) if isinstance(item, dict) else str(item)
            for item in value
        ) or "None"
    if isinstance(value, dict):
        return ", ".join(f"{_humanise(k)}: {v}" for k, v in value.items() if v not in (None, ""))
    return str(value)


# --- inlines ------------------------------------------------------------------

class DocumentRequestInline(UnfoldTabularInline):
    """
    Ask the applicant for something. Add a row and save: they are emailed a
    link to upload it in their portal. To ask again, set the status to "Needs
    another upload" and say what was wrong in the note - they are emailed that.
    """

    model = DocumentRequest
    extra = 0
    tab = True
    verbose_name = "document request"
    verbose_name_plural = "Documents requested from the applicant"
    fields = ("kind", "message", "due_at", "status", "review_note", "uploaded_files", "created_at")
    readonly_fields = ("uploaded_files", "created_at")

    @admin.display(description="Files sent")
    def uploaded_files(self, obj):
        if not obj or not obj.pk:
            return "—"
        docs = list(obj.documents.all())
        if not docs:
            return format_html("<span style='{}'>Nothing yet</span>", _MUTED)
        return format_html_join(
            mark_safe("<br>"), "<a href='{}' target='_blank' rel='noopener'>{}</a> ({} KB)",
            ((reverse("secure-application-document", args=[d.id]), d.original_name or d.stored_name, d.size // 1024)
             for d in docs),
        )


class GuarantorInline(UnfoldStackedInline):
    model = Guarantor
    extra = 0
    max_num = 1
    tab = True
    verbose_name_plural = "Guarantor (optional)"
    fields = ("full_name", "relationship", "email", "phone", "monthly_income_cents")


# --- the admin ----------------------------------------------------------------

@admin.register(RentalApplication)
class RentalApplicationAdmin(UnfoldModelAdmin):
    list_display = ("applicant", "status", "property", "household_income", "lease_progress", "move_in_terms", "deadline", "created_at")
    list_filter = ("status",)
    search_fields = ("first_name", "last_name", "email", "cell_phone")
    autocomplete_fields = ("property",)
    actions = ["send_personalized_lease", "countersign_lease"]
    inlines = [DocumentRequestInline, GuarantorInline]

    readonly_fields = (
        "applicant_block", "identity_block", "background_block", "income_block", "documents_block",
        "household_block", "payment_block", "lease_block", "everything_block",
        "move_in_preview", "verified_at", "decision_due_at", "decided_at", "created_at", "updated_at",
    )

    def get_fieldsets(self, request, obj=None):
        # The viewer's PII grant, stashed on this request's own instance so the
        # read-only renderers below can see it without shared state.
        if obj is not None:
            obj._viewer_sees_pii = request.user.is_superuser or can(getattr(request.user, "role", ""), APPLICATION_READ_PII)
        return [
            ("Overview", {
                "classes": ["tab"],
                "fields": (
                    "status", "property", "user", "decision_due_at", "decided_at", "verified_at",
                    "created_at", "updated_at",
                ),
            }),
            ("Applicant", {"classes": ["tab"], "fields": ("applicant_block",)}),
            ("Identity & background", {"classes": ["tab"], "fields": ("identity_block", "background_block")}),
            ("Income", {"classes": ["tab"], "fields": ("income_block",)}),
            ("Household", {"classes": ["tab"], "fields": ("household_block",)}),
            ("Payment", {"classes": ["tab"], "fields": ("payment_block",)}),
            ("Documents", {"classes": ["tab"], "fields": ("documents_block",)}),
            ("Move-in terms", {
                "classes": ["tab"],
                "fields": (
                    "move_in_date", "months_rent_upfront", "security_deposit_cents",
                    "lease_admin_fee_cents", "pet_fee_cents", "move_in_preview",
                ),
            }),
            ("Lease", {
                "classes": ["tab"],
                "description": (
                    "The landlord on the lease is the OWNER of the home - the company or person Skelton "
                    "manages it for. Skelton signs as managing agent. The lease cannot be sent until the owner is set."
                ),
                "fields": (
                    "landlord_company", "landlord_name", "landlord_address", "landlord_email",
                    "landlord_phone", "lease_block",
                ),
            }),
            ("Owner disclosures", {
                "classes": ["tab"],
                "description": (
                    "Facts only the owner can give, which some states require the lease to state. The Lease tab "
                    "lists which ones this home's state needs. Write 'None known' where that is the answer - a blank "
                    "is not an answer, and the lease cannot be sent while a required one is blank."
                ),
                "fields": (
                    "deposit_held_at", "flood_zone_known", "flood_history", "lead_hazards_known", "other_hazards_known",
                ),
            }),
            ("Everything submitted", {"classes": ["tab"], "fields": ("everything_block",)}),
        ]

    # --- list columns ---------------------------------------------------------

    @admin.display(description="Applicant")
    def applicant(self, obj):
        return f"{obj.first_name} {obj.last_name}".strip() or obj.email or str(obj.pk)[:8]

    @admin.display(description="Household income / mo")
    def household_income(self, obj):
        return _money(obj.gross_monthly_income_cents) if obj.gross_monthly_income_cents else "—"

    @admin.display(description="Lease")
    def lease_progress(self, obj):
        if obj.lease_countersigned_at:
            return format_html('<span style="color:#0b6b47;font-weight:600">Fully signed</span>')
        if obj.lease_signed_at:
            return format_html('<span style="color:#0b6b47;font-weight:600">Signed - countersign</span>')
        if obj.lease_sent_at:
            return format_html('<span style="color:#2563eb;font-weight:600">Sent</span>')
        return format_html('<span style="{}">Not sent</span>', _MUTED)

    @admin.display(description="Move-in terms")
    def move_in_terms(self, obj):
        """Approval is refused until these are set; shown so staff see it from the list."""
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

    @admin.display(description="Decision due")
    def deadline(self, obj):
        """The 24-hour promise. It starts at payment verification, not submission."""
        if not obj.decision_due_at:
            return format_html('<span style="{}">clock not started</span>', _MUTED)
        if obj.decided_at:
            return format_html('<span style="color:#0b6b47">decided</span>')
        if obj.is_overdue:
            return format_html('<strong style="color:#b3261e">OVERDUE</strong>')
        return obj.decision_due_at.strftime("%d %b %H:%M")

    # --- tabs -----------------------------------------------------------------

    def _pii(self, obj) -> bool:
        return getattr(obj, "_viewer_sees_pii", False)

    @admin.display(description="")
    def applicant_block(self, obj):
        d = obj.draft_data or {}
        dob = obj.date_of_birth.strftime("%B %d, %Y") if obj.date_of_birth else ""
        return _section("Who they are", _table([
            ("Name", " ".join(x for x in (obj.first_name, obj.middle_name, obj.last_name) if x)),
            ("Date of birth", "On file - see Identity & background" if dob else ""),
            ("Email", obj.email),
            ("Phone", f"{obj.cell_phone} ({d.get('phoneType')})" if d.get("phoneType") and obj.cell_phone else obj.cell_phone),
            ("Prefers to be contacted by", d.get("preferredContactMethod")),
            ("Wants to move in", obj.move_in_date.strftime("%B %d, %Y") if obj.move_in_date else ""),
            ("Applying for", str(obj.property) if obj.property_id else ""),
        ])) + _section("Emergency contact", _table([
            ("Name", d.get("emergencyContactName")),
            ("Relationship", d.get("emergencyContactRelationship")),
            ("Phone", d.get("emergencyContactPhone")),
        ]))

    @admin.display(description="")
    def identity_block(self, obj):
        d = obj.draft_data or {}
        rows = [
            ("ID type", obj.id_type or d.get("idType")),
            ("SSN / ITIN", f"•••-••-{obj.ssn_last4}" if obj.ssn_last4 else "Not on file"),
            ("Has a driver's licence", _yes_no(d.get("hasLicense"))),
            ("Licence state", obj.drivers_license_state),
            ("Licence number", "On file" if obj.drivers_license_number else "Not on file"),
        ]
        if not obj.pk:
            action = ""
        elif self._pii(obj):
            action = format_html(
                "<p style='margin-top:12px'><a class='srg-button' href='{}'>Show full SSN, licence and date of birth</a></p>"
                "<p style='{};margin-top:6px'>Opens a separate page to check against their ID. Each view is recorded in History.</p>",
                reverse("reveal-application-identity", args=[obj.pk]), _MUTED,
            )
        else:
            action = format_html(
                "<p style='{};margin-top:10px'>Full numbers are visible to Admin-role staff only. Ask an admin to check them against the ID.</p>",
                _MUTED,
            )
        return _section("Identification", _table(rows)) + action

    @admin.display(description="")
    def background_block(self, obj):
        d = obj.draft_data or {}
        return _section("Standard questions", _table([
            ("Ever evicted", _yes_no(d.get("hasEviction"))),
            ("Felony conviction", _yes_no(d.get("hasFelony"))),
            ("Filed for bankruptcy", _yes_no(d.get("hasBankruptcy"))),
            ("Their explanation", d.get("backgroundExplanation")),
            ("Active-duty military", _yes_no(obj.is_active_military if obj.is_active_military is not None else d.get("isActiveMilitary"))),
            ("Receives housing assistance", _yes_no(obj.has_housing_assistance if obj.has_housing_assistance is not None else d.get("receivesHousingAssistance"))),
        ])) + format_html(
            "<p style='{};margin-top:8px'>A yes is context, not a refusal. Decisions follow the published criteria.</p>", _MUTED,
        )

    @admin.display(description="")
    def income_block(self, obj):
        d = obj.draft_data or {}
        total = obj.gross_monthly_income_cents
        rows = [
            ("Total monthly household income (before tax)", _money(total) if total else ""),
            ("Per year", _money(total * 12) if total else ""),
        ]
        rent = obj.property.price_cents if obj.property_id else None
        if total and rent:
            rows.append(("Income ÷ base rent", f"{total / rent:.1f}×"))
        # Older drafts carried one primary income instead of a breakdown.
        if d.get("incomeSource") or d.get("employerName"):
            rows += [
                ("Primary source (older form)", d.get("incomeSource")),
                ("Employer", d.get("employerName")),
                ("Months there", d.get("durationMonths")),
            ]
        sources = [s for s in (d.get("incomeSources") or []) if isinstance(s, dict)]
        breakdown = _list_table(
            ["Who", "Source", "Per month", "Employer", "Months received"],
            [(s.get("earnerName"), _SOURCE_LABELS.get(s.get("sourceType"), s.get("sourceType") or s.get("kind")), _money(s.get("monthlyAmountCents")),
              s.get("employerName"), s.get("monthsReceived")) for s in sources],
        )
        g = Guarantor.objects.filter(application=obj).first()
        guarantor = _table([
            ("Name", g.full_name), ("Relationship", g.relationship), ("Email", g.email), ("Phone", g.phone),
            ("Monthly income", _money(g.monthly_income_cents) if g.monthly_income_cents is not None else ""),
        ]) if g else format_html("<span style='{}'>None added (optional). Edit on the Guarantor tab.</span>", _MUTED)
        return (
            _section("Household income", _table(rows))
            + _section("Breakdown the applicant gave (optional)", breakdown)
            + _section("Guarantor", guarantor)
        )

    @admin.display(description="")
    def household_block(self, obj):
        d = obj.draft_data or {}
        vehicles = [v for v in (d.get("vehicles") or []) if isinstance(v, dict)]
        animals = [p for p in (obj.animals or d.get("pets") or []) if isinstance(p, dict)]
        return (
            _section("People", _table([
                ("Adults 18+ (including applicant)", d.get("adultCount")),
                ("Children / dependents", obj.number_of_kids if obj.has_kids else ("None" if obj.has_kids is False else "")),
            ]))
            + _section("Vehicles", _list_table(
                ["Make & model", "Colour", "Plate", "State"],
                [(v.get("makeModel"), v.get("color"), v.get("licensePlate"), v.get("state")) for v in vehicles],
            ))
            + _section("Animals", _list_table(
                ["Type", "Name", "Breed", "Weight (lb)", "Assistance animal"],
                [(p.get("animalType") or p.get("kind"), p.get("name"), p.get("breed"), p.get("weightLbs"),
                  "Yes - never charged pet fees" if p.get("isServiceAnimal") else "No") for p in animals],
            ))
        )

    @admin.display(description="")
    def payment_block(self, obj):
        if not obj.pk:
            return "—"
        rows = []
        for p in obj.payments.all().order_by("-created_at"):
            proof = "—"
            if p.proof_image_url:
                url = reverse("secure-payment-proof-view", kwargs={"filename": Path(p.proof_image_url).name})
                proof = format_html("<a href='{}' target='_blank' rel='noopener'>View receipt</a>", url)
            rows.append((
                timezone.localtime(p.created_at).strftime("%d %b %Y"), _money(p.amount_cents),
                p.get_payment_method_display(), p.get_status_display(), p.reference_id, proof,
                format_html("<a href='{}'>Open</a>", reverse("admin:billing_payment_change", args=[p.pk])),
            ))
        invoices = [
            (i.invoice_number, i.title, _money(i.total_cents), i.get_status_display(),
             i.due_date.strftime("%d %b %Y") if i.due_date else "",
             format_html("<a href='{}'>Open</a>", reverse("admin:billing_invoice_change", args=[i.pk])))
            for i in obj.invoices.all().order_by("-created_at")
        ]
        return (
            _section("Application fee", _table([
                ("Fee charged", _money(obj.application_fee_cents) if obj.application_fee_cents else ""),
                ("Verified paid", _yes_no(obj.is_fee_paid)),
            ]))
            + _section("Payments", _list_table(["Date", "Amount", "Method", "Status", "Reference", "Proof", ""], rows))
            + _section("Invoices", _list_table(["Number", "For", "Total", "Status", "Due", ""], invoices))
        )

    @admin.display(description="")
    def documents_block(self, obj):
        """Every file the applicant sent, as cards a thumb can open on a phone."""
        if not obj.pk:
            return "—"
        docs = list(obj.documents.select_related("request").order_by("-created_at"))
        if not docs:
            return format_html(
                "<p style='{}'>Nothing uploaded yet. To ask for a document, open the "
                "\"Documents requested from the applicant\" tab at the top and add a request.</p>", _MUTED,
            )
        cards = []
        for d in docs:
            url = reverse("secure-application-document", args=[d.id])
            preview = (
                format_html("<a href='{}' target='_blank' rel='noopener'><img class='srg-doc__thumb' src='{}' alt='' loading='lazy'></a>", url, url)
                if d.content_type.startswith("image/") and d.content_type not in ("image/heic", "image/heif")
                else format_html("<div class='srg-doc__icon'>{}</div>", "PDF" if d.content_type == "application/pdf" else "FILE")
            )
            cards.append(format_html(
                "<li class='srg-doc'>{}<div class='srg-doc__body'>"
                "<strong>{}</strong><span class='srg-doc__meta'>{} · {} · {} KB</span>"
                "<span class='srg-doc__actions'><a class='srg-button' href='{}' target='_blank' rel='noopener'>View</a>"
                "<a class='srg-button srg-button--quiet' href='{}?download=1'>Download</a></span></div></li>",
                preview, d.get_kind_display(), d.original_name or d.stored_name,
                timezone.localtime(d.created_at).strftime("%d %b %Y, %H:%M"), max(1, d.size // 1024), url, url,
            ))
        return format_html("<ul class='srg-docs'>{}</ul>", format_html_join("", "{}", ((c,) for c in cards)))

    @admin.display(description="")
    def lease_block(self, obj):
        if not obj.pk:
            return "—"
        terms = build_lease_terms(obj)
        status = _table([
            ("Ready to send", "Yes" if terms["ready"] else format_html(
                "<span style='color:#b3261e'>No - missing {}</span>", ", ".join(terms["missing"]))),
            ("Sent", timezone.localtime(obj.lease_sent_at).strftime("%d %b %Y %H:%M") if obj.lease_sent_at else ""),
            ("Signed", timezone.localtime(obj.lease_signed_at).strftime("%d %b %Y %H:%M") if obj.lease_signed_at else ""),
            ("Signed by (typed name)", obj.lease_signer_name),
            ("From IP", obj.lease_signed_ip),
            ("Lease version", obj.lease_version),
            ("Fingerprint of signed terms", obj.lease_terms_sha256[:16] + "…" if obj.lease_terms_sha256 else ""),
            ("Countersigned", f"{obj.lease_countersigned_name}, {timezone.localtime(obj.lease_countersigned_at):%d %b %Y}"
             if obj.lease_countersigned_at else ""),
            ("Occupants they listed", obj.lease_occupants),
            ("Vehicles they listed", obj.lease_vehicles),
            ("Emergency contact on lease", obj.lease_emergency_contact),
        ])
        signature = ""
        if obj.lease_signature_url.startswith("data:image/"):
            signature = format_html(
                "<p style='margin-top:10px'><img src='{}' alt='Tenant signature' style='max-width:360px;background:#fff;border:1px solid #ddd;border-radius:6px;padding:6px'></p>",
                obj.lease_signature_url,
            )
        return _section("Lease status", status) + signature

    @admin.display(description="")
    def everything_block(self, obj):
        d = obj.draft_data or {}
        extra = sorted((k, v) for k, v in d.items() if k not in _SHOWN_KEYS and v not in (None, "", [], {}))
        answered = sorted((k, v) for k, v in d.items() if k in _SHOWN_KEYS and v not in (None, "", [], {}) and k != "id")
        return (
            _section("Answers not shown on another tab", _table([(_humanise(k), _humanise_value(k, v)) for k, v in extra])
                     if extra else format_html("<span style='{}'>None - every answer is on a tab above.</span>", _MUTED))
            + _section("Every saved answer, as submitted", _table([(_humanise(k), _humanise_value(k, v)) for k, v in answered]))
            + format_html("<p style='{};margin-top:8px'>SSN and licence numbers are never kept in the saved answers; see Identity.</p>", _MUTED)
        )

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
            max_security_deposit_cents=deposit_ceiling_cents(obj.property.state, obj.property.price_cents),
        )
        rows = [(item["description"], _money(item["unit_price_cents"] * item.get("quantity", 1))) for item in breakdown.line_items]
        rows.append((format_html("<strong>Total</strong>"), format_html("<strong>{}</strong>", _money(breakdown.total_cents))))
        warnings = format_html_join("", "<p style='color:#e0a03a;margin:6px 0 0'>{}</p>", ((w,) for w in breakdown.warnings))
        return _table(rows) + warnings

    # --- saving: document request emails --------------------------------------

    def save_formset(self, request, form, formset, change):
        if formset.model is not DocumentRequest:
            return super().save_formset(request, form, formset, change)

        before = {
            r.pk: r.status for r in DocumentRequest.objects.filter(pk__in=[f.instance.pk for f in formset.forms if f.instance.pk])
        }
        instances = formset.save(commit=False)
        for obj in formset.deleted_objects:
            obj.delete()
        for req in instances:
            is_new = req.pk is None or req.pk not in before
            if is_new:
                req.requested_by = request.user
            req.save()
            if is_new:
                email_request(req)
                messages.success(request, f"Asked {req.application.email or 'the applicant'} for: {req.get_kind_display()}.")
            elif req.status == DocumentRequestStatus.REJECTED and before.get(req.pk) != DocumentRequestStatus.REJECTED:
                email_rejection(req)
                messages.info(request, f"Asked again for: {req.get_kind_display()}, with your note.")
        formset.save_m2m()

    # --- actions --------------------------------------------------------------

    @admin.action(description="Send lease to sign (approved applications only)")
    def send_personalized_lease(self, request, queryset):
        link = f"{settings.PUBLIC_SITE_URL.rstrip('/')}/portal/lease"
        sent, skipped = 0, []
        for app in queryset.select_related("property"):
            name = f"{app.first_name} {app.last_name}".strip() or app.email or str(app.pk)[:8]
            if app.status not in (ApplicationStatus.APPROVED, ApplicationStatus.APPROVED_WITH_CONDITIONS):
                skipped.append(f"{name} (not approved)")
                continue
            terms = build_lease_terms(app)
            if not terms["ready"]:
                skipped.append(f"{name} (missing {', '.join(terms['missing'])})")
                continue
            if app.lease_signed_at:
                skipped.append(f"{name} (already signed)")
                continue
            app.lease_sent_at = timezone.now()
            app.save(update_fields=["lease_sent_at", "updated_at"])
            if app.email:
                queue_email(
                    to_email=app.email,
                    subject=f"Your lease for {app.property} is ready to sign",
                    body_text=(
                        f"Hi {app.first_name or 'there'},\n\n"
                        f"Your lease for {app.property} is ready. Sign in to your portal to read it and sign:\n\n"
                        f"  {link}\n\n"
                        f"Landlord: {terms['landlord']['owner']}, managed by Skelton Realty Group.\n"
                        "You can ask for a paper copy at no charge - just reply to this email.\n"
                    ),
                    template="lease-ready",
                )
            sent += 1
        if sent:
            messages.success(request, f"Lease sent to {sent} applicant(s). They sign in their portal: {link}")
        if skipped:
            messages.warning(request, "Not sent: " + "; ".join(skipped))

    @admin.action(description="Countersign lease as managing agent")
    def countersign_lease(self, request, queryset):
        done, skipped = 0, []
        for app in queryset:
            name = f"{app.first_name} {app.last_name}".strip() or str(app.pk)[:8]
            if not app.lease_signed_at:
                skipped.append(f"{name} (tenant has not signed)")
                continue
            if app.lease_countersigned_at:
                skipped.append(f"{name} (already countersigned)")
                continue
            app.lease_countersigned_by = request.user
            app.lease_countersigned_name = request.user.get_full_name() or request.user.email
            app.lease_countersigned_at = timezone.now()
            app.save(update_fields=["lease_countersigned_by", "lease_countersigned_name", "lease_countersigned_at", "updated_at"])
            done += 1
        if done:
            messages.success(request, f"Countersigned {done} lease(s) for Skelton Realty Group as managing agent.")
        if skipped:
            messages.warning(request, "Not countersigned: " + "; ".join(skipped))
