"""
Application decisions.

APPROVAL ON WORSE TERMS IS ADVERSE ACTION.

FCRA §1681m(a) requires a notice when a decision is based wholly or partly on a
consumer report, and the statute covers approval on terms LESS FAVOURABLE than
those requested — not only outright denial. The tier-two track approves people
with a larger deposit or a co-signer BECAUSE OF what the report showed, so it
generates notices on approvals. That reads as a contradiction until you notice
the wording, which is exactly why it is enforced here rather than left to
whoever builds the admin screen.
"""

from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .models import AdverseActionNotice, ApplicationStatus, RentalApplication
from .move_in import calculate_move_in

# A plain approval needs no notice. These two do, when a report informed them.
NOTICE_WORTHY = {ApplicationStatus.REJECTED, ApplicationStatus.APPROVED_WITH_CONDITIONS}
APPROVALS = {ApplicationStatus.APPROVED, ApplicationStatus.APPROVED_WITH_CONDITIONS}


@dataclass
class DecisionResult:
    application: RentalApplication
    adverse_action_notice: AdverseActionNotice | None = None
    invoice: object | None = None
    warnings: list[str] | None = None


@transaction.atomic
def decide_application(
    application: RentalApplication,
    *,
    decision: str,
    reason: str,
    based_on_consumer_report: bool,
    actor,
    agency_name: str = "",
    agency_contact: str = "",
) -> DecisionResult:
    from apps.billing.models import Invoice, InvoiceStatus

    if not reason.strip():
        # Applies to approvals too: someone approved with a larger deposit is
        # owed the reason as much as someone declined.
        raise ValueError("A decision requires a reason the applicant can be told.")
    if application.is_decided:
        raise ValueError("This application has already been decided.")

    needs_notice = based_on_consumer_report and decision in NOTICE_WORTHY
    if needs_notice and not (agency_name.strip() and agency_contact.strip()):
        raise ValueError(
            "An adverse action notice needs the consumer reporting agency that supplied the "
            "report, so the applicant can dispute it. FCRA 1681m(a)."
        )

    application.status = decision
    application.decision_reason = reason.strip()
    application.decided_at = timezone.now()
    application.save(update_fields=["status", "decision_reason", "decided_at", "updated_at"])

    notice = None
    if needs_notice:
        notice = AdverseActionNotice.objects.create(
            rental_application=application, reason=reason.strip(),
            agency_name=agency_name.strip(), agency_contact=agency_contact.strip(),
        )

    invoice = None
    warnings: list[str] = []
    if decision in APPROVALS:
        if not application.property:
            raise ValueError("Cannot invoice a move-in without the property the applicant is moving into.")

        # THE MOVE-IN TERMS MUST BE SET DELIBERATELY, NOT INHERITED.
        #
        # `calculate_move_in` falls back to one month's rent for a missing
        # deposit and to the configured default for a missing admin fee. Those
        # fallbacks are reasonable for a quote and completely wrong for an
        # invoice: approving without touching them charges a real person real
        # money that nobody chose for their application, on a site whose whole
        # position is that nothing appears for the first time at checkout.
        #
        # So approval requires them to have been entered. Declining does not —
        # there is nothing to charge.
        unset = []
        if application.security_deposit_cents is None:
            unset.append("security deposit")
        if application.lease_admin_fee_cents is None:
            unset.append("administration fee")
        if application.has_pets and application.pet_fee_cents is None:
            unset.append("pet fee (this application declares pets)")
        if unset:
            raise ValueError(
                "Set the move-in terms before approving: "
                + ", ".join(unset)
                + ". Enter 0 where nothing is charged — a blank is not the same as free.",
            )
        breakdown = calculate_move_in(
            monthly_rent_cents=application.property.price_cents,
            months_upfront=application.months_rent_upfront,
            security_deposit_cents=application.security_deposit_cents,
            # Already collected at application time, so not charged again.
            application_fee_cents=0 if application.is_fee_paid else application.application_fee_cents,
            lease_admin_fee_cents=application.lease_admin_fee_cents,
            # Only when the application actually declared a pet. A pet fee on
            # an application with no pet is a charge nobody can justify.
            pet_fee_cents=application.pet_fee_cents if application.has_pets else 0,
            max_security_deposit_cents=deposit_ceiling_cents(
                application.property.state, application.property.price_cents,
            ),
        )
        warnings = breakdown.warnings
        invoice = Invoice.objects.create(
            title="Move-in costs", user=application.user, rental_application=application,
            due_date=timezone.localdate() + timedelta(days=7),
            line_items=breakdown.line_items,
            # SENT, not DRAFT. A draft is invisible to the resident's portal by
            # design, so approving used to produce an invoice nobody could see
            # or pay — the approval email would point at an empty payments page.
            status=InvoiceStatus.SENT,
        )
        queue_approval_email(application, invoice, breakdown)

    return DecisionResult(application=application, adverse_action_notice=notice, invoice=invoice, warnings=warnings)


def deposit_ceiling_cents(state: str, monthly_rent_cents: int) -> int | None:
    """
    The legal maximum security deposit for this state, in cents.

    Returns None when nothing is configured, which makes the breakdown say the
    ceiling has not been checked rather than implying it has. Several states cap
    the deposit at a multiple of one month's rent and the multiple differs, so
    the table is per-state and is filled in by the business and its counsel —
    guessing a national number here would be worse than admitting we do not
    know one.
    """
    from django.conf import settings

    table = getattr(settings, "SECURITY_DEPOSIT_MAX_MONTHS", {}) or {}
    months = table.get((state or "").upper())
    if months is None:
        months = getattr(settings, "SECURITY_DEPOSIT_MAX_MONTHS_DEFAULT", None)
    if months is None:
        return None
    return int(round(monthly_rent_cents * float(months)))


def queue_approval_email(application, invoice, breakdown) -> None:
    """
    Tell the applicant they are approved, what is due, and where to pay it.

    ITEMISED IN THE EMAIL ITSELF, not just linked. Someone deciding whether they
    can afford to move needs the number in front of them, and an email that says
    only "log in to see your costs" is the pattern this business exists not to
    use. The portal link is for paying, not for finding out what you owe.
    """
    from apps.integrations.models import queue_email

    to_email = (application.email or "").strip()
    if not to_email:
        return

    lines = [
        f"  {item['description']}"
        + (f" x{item['quantity']}" if item.get("quantity", 1) > 1 else "")
        + f"  ${item['unit_price_cents'] * item.get('quantity', 1) / 100:,.2f}"
        for item in breakdown.line_items
    ]
    portal_url = f"{settings.PUBLIC_SITE_URL.rstrip('/')}/portal/payments"
    home = str(application.property) if application.property_id else "your home"

    queue_email(
        to_email=to_email,
        subject=f"Your application for {home} has been approved",
        body_text=(
            f"Good news — your application for {home} has been approved.\n\n"
            "Here is what is due before move-in:\n\n"
            + "\n".join(lines)
            + f"\n\n  Total  ${breakdown.total_cents / 100:,.2f}\n\n"
            f"You can pay it here: {portal_url}\n\n"
            f"Reference {invoice.invoice_number} when you send payment, and upload your "
            "receipt on that page so we can match it quickly.\n\n"
            "Once it clears we will book you in to sign the lease and collect the keys.\n"
        ),
        template="application-approved",
    )


def link_applications_to_user(user) -> int:
    """
    Attach applications made before the account existed.

    Someone applies as a guest — that is deliberate, the flow does not demand an
    account up front — and then registers so they can track it and pay. Without
    this their own application is invisible to them: the portal filters by
    `user`, and a guest application has none.

    ONLY ON A VERIFIED EMAIL. The caller must have proved the address, because
    the match is by email and nothing else — linking on an unverified one would
    let anybody type a stranger's address at registration and inherit their
    application, which contains a date of birth and an income history.

    Only unclaimed applications are touched, so this can never move one from a
    person who already owns it.
    """
    if not user or not getattr(user, "is_email_verified", False):
        return 0

    email = (user.email or "").strip().lower()
    if not email:
        return 0

    return RentalApplication.objects.filter(
        email__iexact=email, user__isnull=True,
    ).update(user=user)


def overdue_applications():
    """Applications whose promised deadline has passed without a decision."""
    return RentalApplication.objects.filter(
        decision_due_at__isnull=False, decided_at__isnull=True, decision_due_at__lt=timezone.now(),
    )


def abandoned_drafts(older_than_hours: int = 4):
    """Drafts worth one recovery email. Only once, and only if reachable."""
    cutoff = timezone.now() - timedelta(hours=older_than_hours)
    return RentalApplication.objects.filter(
        status=ApplicationStatus.DRAFT, recovery_email_sent=False, updated_at__lt=cutoff,
    ).exclude(email="")
