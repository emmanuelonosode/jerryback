"""
The residential lease: its terms, who may see them, and how signing is recorded.

EVERY NUMBER COMES FROM THE APPLICATION OR FROM SETTINGS. The previous version
filled gaps with a sample: a Deer Park house, $1,596 rent, 2024 dates, a named
landlord who was not the owner, and "any property at all" when the application
had none. A lease is a contract; a plausible default in it is a term nobody
agreed to. So a missing value is reported in `missing`, the page shows it as
TO CONFIRM, and the lease cannot be signed until it is filled in.

THE LANDLORD IS THE OWNER. Homes belong to companies and individual investors;
Skelton Realty Group manages them and signs as managing agent. Staff enter the
owner per application (`landlord_company` and friends).

PRIVATE TO THE TENANT. These endpoints used to be open to anyone, and `latest`
returned the newest applicant's name, email, address and signature to an
anonymous caller. Now: signed in, and either the applicant themselves or staff
holding `application:read`.
"""

import hashlib
import json
from datetime import date

from django.conf import settings
from django.db.models import Q
from django.utils import timezone
from rest_framework import status as http
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.accounts.permissions import APPLICATION_READ, can
from apps.core.money import format_usd
from apps.properties.models import FeeCadence, FeeCondition, Property, is_rent_restatement

from .lease_states import rule_for
from .lease_text import build_sections
from .models import ApplicationStatus, Guarantor, RentalApplication

#: Bumped whenever the clause text in the web lease changes, so a signature is
#: always tied to the wording that was on screen.
LEASE_VERSION = "2026-09-state"

#: What the tenant ticks before signing. Stored verbatim with the signature.
ESIGN_CONSENT = (
    "I agree to sign this lease electronically, and that my electronic signature is the legal "
    "equivalent of my handwritten signature. I have reviewed the whole lease. I understand I can "
    "ask for a paper copy at no charge, and can withdraw my consent to electronic signing by "
    "contacting the managing agent before I sign."
)

SIGNABLE_STATUSES = {ApplicationStatus.APPROVED, ApplicationStatus.APPROVED_WITH_CONDITIONS}

STATE_NAMES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California",
    "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware", "DC": "the District of Columbia",
    "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois",
    "IN": "Indiana", "IA": "Iowa", "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana",
    "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota",
    "MS": "Mississippi", "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
    "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
    "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma", "OR": "Oregon",
    "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota",
    "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont", "VA": "Virginia",
    "WA": "Washington", "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
}

_WORDS = (
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen "
    "fifteen sixteen seventeen eighteen nineteen twenty"
).split()


def state_of(prop) -> str:
    return (prop.state or "").upper() if prop else ""


def in_words(n) -> str:
    """3 -> 'three (3)', 2.5 -> 'two and a half (2.5)'. What a lease says."""
    if n is None:
        return ""
    whole = int(n)
    half = n - whole >= 0.5
    word = _WORDS[whole] if whole < len(_WORDS) else str(whole)
    figure = f"{whole}.5" if half else str(whole)
    return f"{word}{' and a half' if half else ''} ({figure})"


def one_year_term(start: date) -> date:
    """The day before the same date next year. 29 Feb starts end on 28 Feb."""
    try:
        anniversary = start.replace(year=start.year + 1)
    except ValueError:  # 29 February
        anniversary = start.replace(year=start.year + 1, day=28)
        return anniversary
    return date.fromordinal(anniversary.toordinal() - 1)


def late_fee_cents(monthly_rent_cents: int) -> int:
    """
    The lesser of the flat cap and the percentage of monthly rent.

    Chosen to sit under every state limit in the catalogue - NV caps at 5%,
    NC at the greater of $15 or 5%, CO at the greater of $50 or 5% - so no
    tenant anywhere is charged more than their state allows by this clause.
    """
    percent = monthly_rent_cents * settings.LEASE_LATE_FEE_BASIS_POINTS // 10000
    return min(settings.LEASE_LATE_FEE_CAP_CENTS, percent)


def _date(d) -> str:
    return f"{d:%B} {d.day}, {d.year}" if d else ""


def build_lease_terms(app: RentalApplication) -> dict:
    """Everything the lease says, and what is still missing before it can be signed."""
    missing: list[str] = []
    prop = (
        Property.objects.with_total_monthly().prefetch_related("fees").filter(pk=app.property_id).first()
        if app.property_id else None
    )
    if prop is None:
        missing.append("the home this lease is for")
    if not app.landlord_company.strip():
        missing.append("the owner (landlord) of the home")
    if app.move_in_date is None:
        missing.append("the move-in date")
    if app.security_deposit_cents is None:
        missing.append("the security deposit")

    draft = app.draft_data or {}
    service_animals = [p for p in (app.animals or []) if isinstance(p, dict) and p.get("isServiceAnimal")]
    pets = [p for p in (app.animals or []) if isinstance(p, dict) and not p.get("isServiceAnimal")]

    fees = []
    base_rent = total_rent = None
    if prop is not None:
        base_rent = prop.price_cents
        total_rent = prop.total_monthly_cents
        fees = [
            {"label": f.label, "amount": format_usd(f.amount_cents)}
            for f in prop.fees.all()
            if f.cadence == FeeCadence.MONTHLY and f.condition == FeeCondition.REQUIRED
            and not is_rent_restatement(f.label)
        ]
        # Pet rent is a conditional monthly fee on the listing. It belongs on
        # the lease only when the household has a pet - never for an
        # assistance animal - and at the amount the listing showed.
        if pets:
            pet_rent = next(
                (f for f in prop.fees.all()
                 if f.cadence == FeeCadence.MONTHLY and f.condition != FeeCondition.REQUIRED
                 and "pet" in f"{f.fee_key} {f.label}".lower()),
                None,
            )
            if pet_rent is not None:
                pet_rent_cents = pet_rent.amount_cents * len(pets) if "per pet" in (pet_rent.label + pet_rent.reason).lower() else pet_rent.amount_cents
                fees.append({"label": f"{pet_rent.label} ({len(pets)} pet{'s' if len(pets) != 1 else ''})", "amount": format_usd(pet_rent_cents)})
                total_rent = (total_rent or 0) + pet_rent_cents
                if state_of(prop) == "CO" and pet_rent_cents > max(3500, (prop.price_cents * 150) // 10000):
                    missing.append("pet rent within Colorado's limit (the greater of $35 or 1.5% of rent)")

    start = app.move_in_date
    state = (prop.state if prop else "").upper()
    rule = rule_for(state)

    # What this state requires the owner to have told us before signing.
    if not app.landlord_address.strip():
        missing.append("the owner's address for notices")
    if rule.deposit_bank_required and not app.deposit_held_at.strip():
        missing.append("where the deposit is held (bank name and address)")
    if rule.flood_zone_required and app.flood_zone_known not in ("yes", "no"):
        missing.append("whether the owner knows the home is in a flood zone")
    if rule.flood_history_required and not app.flood_history.strip():
        missing.append("the owner's flooding history for the home (or 'None known')")
    if rule.other_hazards_required and not app.other_hazards_known.strip():
        missing.append("the owner's other required disclosures (or 'None known')")
    if prop is not None and (prop.year_built is None or prop.year_built < 1978) and not app.lead_hazards_known.strip():
        missing.append("the owner's lead-paint disclosure (or 'None known')")
    if state == "TX" and not settings.LEASE_EMERGENCY_PHONE:
        missing.append("a 24-hour emergency repair number (LEASE_EMERGENCY_PHONE)")

    # Legal ceilings: reported, never silently reduced.
    if prop is not None and app.security_deposit_cents is not None:
        from .services import deposit_ceiling_cents

        ceiling = deposit_ceiling_cents(state, prop.price_cents)
        if ceiling is not None and app.security_deposit_cents > ceiling:
            missing.append(f"a security deposit within {state}'s limit of {format_usd(ceiling)}")
    if state == "CO" and pets and (app.pet_fee_cents or 0) > 30000:
        missing.append("a pet fee within Colorado's $300 pet-deposit limit")
    agent_address = settings.COMPANY_ADDRESS.replace("|", ", ")
    guarantor = Guarantor.objects.filter(application=app).first() if app.pk else None

    terms = {
        "version": LEASE_VERSION,
        "ready": not missing,
        "missing": missing,
        "landlord": {
            "owner": app.landlord_company.strip(),
            "signatory": app.landlord_name.strip(),
            "address": app.landlord_address.strip(),
            "email": app.landlord_email.strip(),
            "phone": app.landlord_phone.strip(),
        },
        "agent": {
            "name": "Skelton Realty Group",
            "address": agent_address,
            "phone": settings.COMPANY_PHONE,
            "email": settings.COMPANY_EMAIL,
        },
        "tenant": {
            "name": f"{app.first_name} {app.last_name}".strip(),
            "email": app.email,
            "phone": app.cell_phone,
        },
        "household": {
            "adults": draft.get("adultCount") or 1,
            "dependents": app.number_of_kids or 0,
            "occupants": app.lease_occupants,
            "vehicles": draft.get("vehicles") or [],
            "pets": pets,
            "assistance_animals": service_animals,
        },
        "premises": {
            "address": f"{prop.address}, {prop.city}, {prop.state} {prop.zip_code}" if prop else "",
            "state": state,
            "state_name": STATE_NAMES.get(state, state),
            "type": prop.get_type_display() if prop else "",
            "bedrooms": in_words(prop.bedrooms) if prop else "",
            "bathrooms": in_words(prop.bathrooms) if prop else "",
            "parking": (prop.parking or (f"{in_words(prop.garage)} garage space(s)" if prop.garage else "")) if prop else "",
            "year_built": prop.year_built if prop else None,
            # Federal rule: pre-1978 housing needs the disclosure. An unknown
            # year gets it too - disclosing on a newer home costs nothing,
            # omitting it on an older one is a violation.
            "lead_paint_disclosure": bool(prop) and (prop.year_built is None or prop.year_built < 1978),
        },
        "term": {
            "start": _date(start),
            "end": _date(one_year_term(start)) if start else "",
        },
        "money": {
            "base_rent": format_usd(base_rent) if base_rent is not None else "",
            "monthly_fees": fees,
            "total_monthly_rent": format_usd(total_rent) if total_rent is not None else "",
            "security_deposit": format_usd(app.security_deposit_cents) if app.security_deposit_cents is not None else "",
            "lease_admin_fee": format_usd(app.lease_admin_fee_cents) if app.lease_admin_fee_cents else "",
            # Never charged for an assistance animal - see the accommodations clause.
            "pet_fee": format_usd(app.pet_fee_cents) if pets and app.pet_fee_cents else "",
            "rent_due_day": settings.LEASE_RENT_DUE_DAY,
            # The uniform grace period, lengthened where a state requires more.
            "late_after_days": max(settings.LEASE_LATE_AFTER_DAYS, rule.min_late_days),
            "late_fee": format_usd(late_fee_cents(total_rent)) if total_rent else "",
            "late_fee_rule": (
                f"the lesser of {format_usd(settings.LEASE_LATE_FEE_CAP_CENTS)} or "
                f"{settings.LEASE_LATE_FEE_BASIS_POINTS / 100:g}% of the monthly rent"
            ),
            "returned_payment_fee": format_usd(settings.LEASE_RETURNED_PAYMENT_FEE_CENTS),
            # The uniform promise, shortened only if a state's deadline is shorter.
            "deposit_return_days": min(settings.LEASE_DEPOSIT_RETURN_DAYS, rule.deposit_return_days),
        },
        "entry_notice_hours": max(settings.LEASE_ENTRY_NOTICE_HOURS, rule.entry_hours),
        "emergency_phone": settings.LEASE_EMERGENCY_PHONE,
        "guarantor": {"name": guarantor.full_name} if guarantor else None,
    }

    fill = {
        "deposit_held_at": app.deposit_held_at.strip() or "TO CONFIRM",
        "flood_history": app.flood_history.strip() or "TO CONFIRM",
        "other_hazards_known": app.other_hazards_known.strip() or "TO CONFIRM",
        "lead_hazards_known": app.lead_hazards_known.strip(),
        "emergency_phone": settings.LEASE_EMERGENCY_PHONE or "TO CONFIRM",
        "flood_zone_sentence": {
            "yes": f"{app.landlord_company or 'The owner'} is aware that the home you are renting is located in a 100-year floodplain / special flood hazard area.",
            "no": f"{app.landlord_company or 'The owner'} is not aware that the home you are renting is located in a 100-year floodplain / special flood hazard area.",
        }.get(app.flood_zone_known, "TO CONFIRM"),
    }
    terms["sections"] = build_sections(terms, rule, fill)
    return terms


def terms_digest(terms: dict) -> str:
    return hashlib.sha256(json.dumps(terms, sort_keys=True, default=str).encode()).hexdigest()


def _signing(app: RentalApplication) -> dict:
    return {
        "is_signed": bool(app.lease_signed_at),
        "signed_at": timezone.localtime(app.lease_signed_at).strftime("%B %d, %Y at %I:%M %p") if app.lease_signed_at else None,
        "signer_name": app.lease_signer_name or None,
        "signature_url": app.lease_signature_url or None,
        "countersigned_by": app.lease_countersigned_name or None,
        "countersigned_at": _date(timezone.localtime(app.lease_countersigned_at).date()) if app.lease_countersigned_at else None,
        "consent_text": ESIGN_CONSENT,
    }


def lease_payload(app: RentalApplication) -> dict:
    # A signed lease shows what was signed, not today's rebuild of it.
    terms = app.lease_terms_snapshot if app.lease_signed_at and app.lease_terms_snapshot else build_lease_terms(app)
    return {
        "application_id": str(app.id),
        "status": app.status,
        "can_sign": (
            app.status in SIGNABLE_STATUSES and bool(app.lease_sent_at)
            and not app.lease_signed_at and terms.get("ready", False)
        ),
        "terms": terms,
        "signing": _signing(app),
        "questionnaire": {
            "occupants": app.lease_occupants,
            "vehicles": app.lease_vehicles,
            "emergency_contact": app.lease_emergency_contact,
        },
    }


def _visible_to(user):
    """Applications this user may open a lease for."""
    qs = RentalApplication.objects.select_related("property", "user")
    if can(getattr(user, "role", ""), APPLICATION_READ):
        return qs
    own = Q(user=user)
    if user.email:
        own |= Q(user__isnull=True, email__iexact=user.email.strip())
    return qs.filter(own)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def lease_agreement_detail(request, application_id):
    app = _visible_to(request.user).filter(id=application_id).first()
    if app is None:
        # 404, not 403: whether someone else's application exists is not ours to say.
        return Response({"detail": "No such lease."}, status=http.HTTP_404_NOT_FOUND)
    return Response(lease_payload(app))


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def lease_agreement_latest(request):
    """The signed-in person's own most recent lease. Never anybody else's."""
    own = Q(user=request.user)
    if request.user.email:
        own |= Q(user__isnull=True, email__iexact=request.user.email.strip())
    app = (
        RentalApplication.objects.select_related("property", "user")
        .filter(own, lease_sent_at__isnull=False)
        .order_by("-lease_sent_at")
        .first()
    )
    if app is None:
        return Response({"detail": "No lease has been sent to you yet."}, status=http.HTTP_404_NOT_FOUND)
    return Response(lease_payload(app))


def _client_ip(request) -> str | None:
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    ip = forwarded.split(",")[0].strip() if forwarded else request.META.get("REMOTE_ADDR")
    return ip or None


#: A drawn signature as a PNG data URL is tens of KB; this rejects anything
#: that is plainly not one without refusing a large high-DPI canvas.
MAX_SIGNATURE_CHARS = 800_000


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def sign_lease_agreement(request, application_id):
    own = Q(user=request.user)
    if request.user.email:
        own |= Q(user__isnull=True, email__iexact=request.user.email.strip())
    # Staff cannot sign on a tenant's behalf, so ownership only.
    app = RentalApplication.objects.select_related("property").filter(own, id=application_id).first()
    if app is None:
        return Response({"detail": "No such lease."}, status=http.HTTP_404_NOT_FOUND)

    if app.lease_signed_at:
        return Response({"detail": "This lease is already signed."}, status=http.HTTP_409_CONFLICT)
    if app.status not in SIGNABLE_STATUSES or not app.lease_sent_at:
        return Response(
            {"detail": "This lease is not ready to sign yet. We will email you when it is."},
            status=http.HTTP_409_CONFLICT,
        )

    data = request.data or {}
    signature = str(data.get("signature_url") or "")
    signer_name = str(data.get("signer_name") or "").strip()[:200]
    if not signature.startswith("data:image/") or len(signature) > MAX_SIGNATURE_CHARS or not signer_name:
        return Response({"detail": "Add your signature and type your full name."}, status=http.HTTP_400_BAD_REQUEST)
    if data.get("consent") is not True:
        return Response({"detail": "Tick the box to agree to sign electronically."}, status=http.HTTP_400_BAD_REQUEST)

    # Recorded before the terms are frozen, so the snapshot names them.
    for key, field, limit in (
        ("occupants", "lease_occupants", 5000),
        ("vehicles", "lease_vehicles", 255),
        ("emergency_contact", "lease_emergency_contact", 255),
    ):
        if key in data:
            setattr(app, field, str(data[key]).strip()[:limit])

    terms = build_lease_terms(app)
    if not terms["ready"]:
        return Response(
            {"detail": "This lease is missing: " + ", ".join(terms["missing"]) + ". We will fix it and let you know."},
            status=http.HTTP_409_CONFLICT,
        )

    app.lease_signature_url = signature
    app.lease_signer_name = signer_name
    app.lease_signed_at = timezone.now()
    app.lease_signed_ip = _client_ip(request)
    app.lease_signed_user_agent = request.META.get("HTTP_USER_AGENT", "")[:400]
    app.lease_consent_text = ESIGN_CONSENT
    app.lease_version = LEASE_VERSION
    app.lease_terms_snapshot = terms
    app.lease_terms_sha256 = terms_digest(terms)
    app.save(update_fields=[
        "lease_signature_url", "lease_signer_name", "lease_signed_at", "lease_signed_ip",
        "lease_signed_user_agent", "lease_consent_text", "lease_version", "lease_terms_snapshot",
        "lease_terms_sha256", "lease_occupants", "lease_vehicles", "lease_emergency_contact", "updated_at",
    ])

    from apps.integrations.alerts import admin_link, describe, notify_staff

    notify_staff(
        subject=f"Lease signed: {signer_name} - {app.property or 'home'}",
        body=describe([
            ("Signed by", signer_name),
            ("Home", str(app.property) if app.property_id else ""),
            ("Signed at", timezone.localtime(app.lease_signed_at).strftime("%a %d %b, %H:%M")),
            ("Open in admin", admin_link(f"crm/rentalapplication/{app.id}/change")),
        ]) + "\n\nCountersign it in the admin (action: Countersign lease).\n",
        kind="lease-signed",
    )
    return Response(lease_payload(app))
