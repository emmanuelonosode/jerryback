"""
What the phone agent is allowed to do.

A CALLER IS AN ANONYMOUS MEMBER OF THE PUBLIC WHO CAN SAY ANYTHING. Every tool
here is reachable by whoever dials the number and talks the model into calling
it, so this list is the same trust level as the public website, plus two
writes the website already accepts anonymously (a tour request and a lead).
Nothing here reads another person's application, a payment, an ID document or
a staff record. That is deliberate, and it is why this is a short list rather
than "the whole admin": a prompt-injected voice agent with staff access is a
data breach that anyone can trigger with a phone call.

Each tool returns plain JSON-serialisable data. Money goes out twice - integer
cents, which is the house unit, and a display string, which is what the model
should actually say aloud so it never does arithmetic on a price.
"""

from datetime import timedelta

from django.conf import settings
from django.http import QueryDict
from django.utils import timezone
from django.utils.dateparse import parse_date

from apps.core.money import format_usd
from apps.crm.models import (
    DECIDED_STATUSES, ApplicationStatus, ActivityType, Lead, LeadActivity, LeadSource,
    LeadStatus, RentalApplication,
)
from apps.integrations.alerts import admin_link, deliver_in_background, describe, notify_staff
from apps.integrations.models import queue_email
from apps.properties.models import Property, is_rent_restatement
from apps.properties.views import _apply_filters
from apps.scheduler.models import TourRequest, TourStatus
from apps.scheduler.views import RESPONSE_HOURS

MAX_RESULTS = 10
# How far ahead a tour can be requested. A date three months out is almost
# always a misheard month, and a person will not hold a slot that long anyway.
TOUR_HORIZON_DAYS = 60
TOUR_KINDS = ("in-person", "video")
DAY_PARTS = ("morning", "midday", "afternoon", "evening")


class ToolError(Exception):
    """A refusal the model should relay to the caller, not a server fault."""


# --- helpers -----------------------------------------------------------------

def _digits(phone: str) -> str:
    digits = "".join(c for c in (phone or "") if c.isdigit())
    # +1 (704) 555-0100 and 704-555-0100 are the same number.
    return digits[1:] if len(digits) == 11 and digits.startswith("1") else digits


def _listing_url(home) -> str:
    return f"{settings.PUBLIC_SITE_URL.rstrip('/')}/homes-for-rent/{home.slug}"


def _find_home(slug: str = "", address: str = ""):
    homes = Property.objects.public().with_total_monthly().prefetch_related("fees", "amenities")
    if slug:
        home = homes.filter(slug=slug.strip()).first()
        if home is not None:
            return home
    if address:
        params = QueryDict(mutable=True)
        params["q"] = address
        matched, _ = _apply_filters(_Params(params), homes)
        return matched.first()
    return None


class _Params:
    """The one attribute `_apply_filters` reads off a request."""

    def __init__(self, query_params):
        self.query_params = query_params


def _summary(home) -> dict:
    return {
        "slug": home.slug,
        "address": f"{home.address}, {home.city}, {home.state} {home.zip_code}",
        "bedrooms": home.bedrooms,
        "bathrooms": home.bathrooms,
        "sqft": home.sqft or None,
        "total_monthly_cents": home.total_monthly_cents,
        "total_monthly": format_usd(home.total_monthly_cents),
        "base_rent": format_usd(home.price_cents),
        "status": home.get_status_display(),
        "available_from": home.available_from.isoformat() if home.available_from else "now",
        "pets_allowed": home.pets_allowed,
        "voucher_accepted": home.voucher_accepted,
        "listing_url": _listing_url(home),
    }


def _lead_for(*, phone: str, email: str, name: str) -> Lead:
    """
    Same dedupe rules as the public forms: email when we have one, otherwise
    phone. Never match on a blank - that folds every phone-only lead into one.
    """
    lead = None
    if email:
        lead = Lead.objects.filter(email__iexact=email).first()
    if lead is None and phone:
        lead = Lead.objects.filter(phone=phone).first()
    if lead is None:
        return Lead.objects.create(
            full_name=name or "Phone caller", email=email, phone=phone,
            source=LeadSource.PHONE_AGENT, status=LeadStatus.NEW,
        )
    changed = []
    if name and lead.full_name in ("", "Callback request", "Phone caller"):
        lead.full_name, changed = name, changed + ["full_name"]
    if phone and not lead.phone:
        lead.phone, changed = phone, changed + ["phone"]
    if email and not lead.email:
        lead.email, changed = email, changed + ["email"]
    if changed:
        lead.save(update_fields=[*changed, "updated_at"])
    return lead


# --- tools -------------------------------------------------------------------

def search_homes(args: dict) -> dict:
    params = QueryDict(mutable=True)
    for key in ("query", "city", "state"):
        if args.get(key):
            params["q" if key == "query" else key] = str(args[key])
    if args.get("min_bedrooms") is not None:
        params["min_bedrooms"] = str(int(args["min_bedrooms"]))
    if args.get("min_bathrooms") is not None:
        params["min_bathrooms"] = str(int(args["min_bathrooms"]))
    if args.get("max_monthly_dollars") is not None:
        params["max_price_cents"] = str(int(float(args["max_monthly_dollars"]) * 100))
    if args.get("available_by"):
        params["available_by"] = str(args["available_by"])
    if args.get("pets_allowed"):
        params["pets_allowed"] = "true"
    if args.get("voucher_accepted"):
        params["voucher_accepted"] = "true"

    limit = max(1, min(int(args.get("limit") or 5), MAX_RESULTS))
    queryset, searched = _apply_filters(
        _Params(params), Property.objects.rentable().with_total_monthly(),
    )
    if not searched:
        queryset = queryset.order_by("total_monthly_cents", "id")
    homes = list(queryset[: limit + 1])
    return {
        "count_shown": min(len(homes), limit),
        "more_available": len(homes) > limit,
        "homes": [_summary(h) for h in homes[:limit]],
    }


def get_home_details(args: dict) -> dict:
    home = _find_home(args.get("slug") or "", args.get("address") or "")
    if home is None:
        raise ToolError("No home on the market matches that. Search again or ask the caller to spell the street.")

    fees = [
        {
            "label": f.label,
            "amount": format_usd(f.amount_cents),
            "cadence": f.get_cadence_display(),
            "condition": f.get_condition_display(),
            "applies_when": f.applies_when,
        }
        for f in home.fees.all()
        if not is_rent_restatement(f.label)
    ]
    return {
        **_summary(home),
        "type": home.get_type_display(),
        "description": (home.description or "")[:1500],
        "year_built": home.year_built,
        "neighborhood": home.neighborhood,
        "pet_policy": home.pet_policy,
        "parking": home.parking,
        "laundry": home.laundry,
        "hvac": home.hvac,
        "flooring": home.flooring,
        "appliances": home.appliances or [],
        "has_pool": home.has_pool,
        "accessibility_features": home.accessibility_features or [],
        "amenities": [a.name for a in home.amenities.all()],
        "schools": home.schools or [],
        "fees": fees,
        "self_tour_available": home.allow_selfshow,
        "application_fee": format_usd(settings.APPLICATION_FEE_CENTS),
        "last_verified_at": home.last_verified_at.isoformat() if home.last_verified_at else None,
    }


def book_tour(args: dict) -> dict:
    name = (args.get("full_name") or "").strip()[:200]
    phone = (args.get("phone") or "").strip()[:20]
    email = (args.get("email") or "").strip()
    if not name:
        raise ToolError("Ask the caller for their name before booking.")
    if len(_digits(phone)) < 10:
        raise ToolError("A ten-digit phone number is required so staff can confirm the time.")

    when = parse_date(str(args.get("preferred_date") or ""))
    today = timezone.localdate()
    if when is None:
        raise ToolError("preferred_date must be a date in YYYY-MM-DD form.")
    if when < today:
        raise ToolError("That date has already passed. Confirm the date with the caller.")
    if when > today + timedelta(days=TOUR_HORIZON_DAYS):
        raise ToolError(f"Tours can be requested up to {TOUR_HORIZON_DAYS} days ahead. Confirm the date with the caller.")

    time_of_day = (args.get("preferred_time") or "").strip().lower()
    if time_of_day and time_of_day not in DAY_PARTS:
        raise ToolError(f"preferred_time must be one of: {', '.join(DAY_PARTS)}.")
    kind = (args.get("tour_type") or "in-person").strip().lower()
    if kind not in TOUR_KINDS:
        raise ToolError(f"tour_type must be one of: {', '.join(TOUR_KINDS)}.")

    home = _find_home(args.get("home_slug") or "")
    if home is None:
        raise ToolError("That home is not on the market. Use search_homes to find its slug first.")

    lead = _lead_for(phone=phone, email=email, name=name)
    if lead.property_interest_id is None:
        lead.property_interest = home
        lead.save(update_fields=["property_interest", "updated_at"])
    tour = TourRequest.objects.create(
        lead=lead, property=home,
        # A person confirms the time, exactly as with a tour booked on the site.
        status=TourStatus.PENDING_REVIEW,
        full_name=name, email=email, phone=phone,
        preferred_date=when, preferred_time=time_of_day, tour_type=kind,
        notes=("Booked by the phone agent. " + (args.get("notes") or "")).strip()[:2000],
    )
    LeadActivity.objects.create(
        lead=lead, activity_type=ActivityType.VIEWING_BOOKED,
        note=f"Phone agent requested a {kind} tour of {home} for {when:%a %d %b} {time_of_day}".strip(),
    )

    label = when.strftime("%a %d %b") + (f", {time_of_day}" if time_of_day else "")
    notify_staff(
        subject=f"Tour request (phone): {name} - {home}",
        body=describe([
            ("Name", name), ("Phone", phone), ("Email", email),
            ("Home", str(home)), ("Wants to visit", label), ("Type", kind),
            ("Notes", tour.notes),
            ("Open in admin", admin_link(f"scheduler/tourrequest/{tour.id}/change")),
        ]) + (
            f"\n\nBooked over the phone by the AI agent. The caller was told a person "
            f"would confirm within {RESPONSE_HOURS} business hours.\n"
        ),
        kind="tour",
    )
    if email:
        confirmation = queue_email(
            send_now=False,
            to_email=email,
            subject=f"We have your tour request for {home}",
            body_text=(
                f"Hi {name.split(' ')[0]},\n\n"
                f"Thanks for calling about {home}. Here is what you asked for:\n\n"
                f"  When you would like to visit: {label}\n"
                f"  Type of visit: {kind}\n\n"
                f"Someone will confirm the time with you within {RESPONSE_HOURS} business hours.\n"
                f"The listing: {_listing_url(home)}\n"
            ),
            template="tour-received",
        )
        if confirmation is not None:
            deliver_in_background([confirmation])

    return {
        "reference": str(tour.public_id)[:8].upper(),
        "status": "requested",
        "home": str(home),
        "requested_for": label,
        "tell_the_caller": (
            f"The tour is requested, not yet confirmed. A person will call or email to "
            f"confirm the exact time within {RESPONSE_HOURS} business hours."
        ),
    }


def save_caller_details(args: dict) -> dict:
    phone = (args.get("phone") or "").strip()[:20]
    email = (args.get("email") or "").strip()
    name = (args.get("full_name") or "").strip()[:200]
    summary = (args.get("summary") or "").strip()
    if len(_digits(phone)) < 10 and not email:
        raise ToolError("A phone number or email is needed to save the caller.")
    if not summary:
        raise ToolError("Include a short summary of what the caller wants.")

    lead = _lead_for(phone=phone, email=email, name=name)
    changed = []
    for field, key in (("preferred_location", "preferred_location"), ("has_voucher", "has_voucher"),
                       ("has_pets", "has_pets")):
        if args.get(key) is not None:
            setattr(lead, field, args[key])
            changed.append(field)
    if args.get("max_monthly_dollars") is not None:
        lead.budget_max_cents = int(float(args["max_monthly_dollars"]) * 100)
        changed.append("budget_max_cents")
    if args.get("move_in_timeline"):
        lead.move_in_timeline = args["move_in_timeline"]
        changed.append("move_in_timeline")
    lead.message = summary[:2000]
    lead.status = LeadStatus.NEW
    lead.save(update_fields=[*changed, "message", "status", "updated_at"])

    # NOTE, not CALL: CALL marks a human as having made contact and suppresses
    # the follow-up nudge, and a staff member still owes this person a call.
    LeadActivity.objects.create(
        lead=lead, activity_type=ActivityType.NOTE, note=f"Phone agent call: {summary}"[:4000],
    )

    wants_person = bool(args.get("needs_human_followup"))
    if wants_person:
        notify_staff(
            subject=f"Call back (phone agent): {lead.full_name} - {phone or email}",
            body=describe([
                ("Name", lead.full_name), ("Phone", phone), ("Email", email),
                ("Open in admin", admin_link(f"crm/lead/{lead.id}/change")),
            ]) + f"\n\nThe AI agent could not finish this call and promised a person would follow up.\n\n{summary}\n",
            kind="callback",
        )
    return {
        "saved": True,
        "staff_notified": wants_person,
        "tell_the_caller": (
            "A member of the team will follow up with you." if wants_person else "Your details are saved."
        ),
    }


def check_application_status(args: dict) -> dict:
    """
    BOTH the email and the phone on the application must match. Caller ID can
    be spoofed and emails can be guessed; requiring the pair raises the bar,
    and the answer is still only the stage - never a reason, an amount owed
    or anything from the application itself. Declines are not read out: the
    written notice is the record, and a phone line is the wrong place for it.
    """
    email = (args.get("email") or "").strip()
    phone = _digits(args.get("phone") or "")
    if not email or len(phone) < 10:
        raise ToolError("Both the email and the phone number on the application are needed.")

    candidates = (
        RentalApplication.objects.filter(email__iexact=email)
        .exclude(status=ApplicationStatus.DRAFT)
        .select_related("property")
        .order_by("-created_at")[:5]
    )
    match = next((a for a in candidates if _digits(a.cell_phone) == phone), None)
    if match is None:
        return {
            "found": False,
            "tell_the_caller": (
                "I could not find a submitted application with that email and phone number. "
                "They can check at the website under Apply, then Status, or a person can help."
            ),
        }

    stage = {
        ApplicationStatus.PENDING_PAYMENT: "Waiting on the application fee.",
        ApplicationStatus.PENDING_VERIFICATION: "The fee payment was reported and is being verified.",
        ApplicationStatus.SUBMITTED: "Submitted and in the review queue.",
        ApplicationStatus.REVIEWED: "Reviewed; a decision is being finalised.",
        ApplicationStatus.APPROVED: "Approved. Next steps and the lease were sent by email.",
        ApplicationStatus.APPROVED_WITH_CONDITIONS: "Approved with conditions. The details were sent by email.",
    }.get(match.status, "A decision was made and sent by email.")
    return {
        "found": True,
        "home": str(match.property) if match.property_id else None,
        "stage": stage,
        "decided": match.status in DECIDED_STATUSES,
        "decision_due_by": (
            timezone.localtime(match.decision_due_at).strftime("%a %d %b, %I:%M %p")
            if match.decision_due_at and match.status not in DECIDED_STATUSES else None
        ),
    }


def get_leasing_policies(_args: dict) -> dict:
    site = settings.PUBLIC_SITE_URL.rstrip("/")
    return {
        "application_fee": format_usd(settings.APPLICATION_FEE_CENTS),
        "lease_admin_fee": format_usd(settings.MOVE_IN_LEASE_ADMIN_FEE_CENTS),
        "application_decision_hours": settings.DECISION_WINDOW_HOURS,
        "tour_confirmation_hours": RESPONSE_HOURS,
        "how_to_apply": f"Online at {site}/apply, from any listing page.",
        "qualification_criteria_page": f"{site}/qualifications",
        "fees_page": f"{site}/fees",
        "housing_vouchers_page": f"{site}/housing-vouchers",
        "fair_housing_page": f"{site}/fair-housing",
    }


# --- registry ----------------------------------------------------------------

_STR = {"type": "string"}
_BOOL = {"type": "boolean"}

TOOLS = {
    "search_homes": {
        "handler": search_homes,
        "description": (
            "Search homes currently for rent. Every filter is optional. Prices are the all-in "
            "monthly total including required monthly fees. Returns at most 10 homes; read out "
            "two or three, not all of them."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {**_STR, "description": "Free text: street, ZIP, neighborhood or city."},
                "city": _STR,
                "state": {**_STR, "description": "Two-letter state code, e.g. TX."},
                "min_bedrooms": {"type": "integer", "minimum": 0},
                "min_bathrooms": {"type": "integer", "minimum": 0},
                "max_monthly_dollars": {"type": "number", "minimum": 0},
                "available_by": {**_STR, "description": "Move-in date, YYYY-MM-DD."},
                "pets_allowed": _BOOL,
                "voucher_accepted": {**_BOOL, "description": "Only homes that accept housing vouchers."},
                "limit": {"type": "integer", "minimum": 1, "maximum": MAX_RESULTS},
            },
        },
    },
    "get_home_details": {
        "handler": get_home_details,
        "description": (
            "Everything published about one home: fees, pet policy, parking, appliances, "
            "amenities, schools, availability. Pass the slug from search_homes, or an address."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"slug": _STR, "address": _STR},
        },
    },
    "book_tour": {
        "handler": book_tour,
        "description": (
            "Request a tour of a home. This creates a request that a person confirms; tell the "
            "caller it is requested, not confirmed. Confirm the date and phone number back to "
            "the caller before calling this."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "home_slug": {**_STR, "description": "The slug from search_homes or get_home_details."},
                "full_name": _STR,
                "phone": {**_STR, "description": "Ten-digit US number. Use the caller's number if they agree."},
                "email": {**_STR, "description": "Optional. If given, a confirmation email is sent."},
                "preferred_date": {**_STR, "description": "YYYY-MM-DD."},
                "preferred_time": {"type": "string", "enum": list(DAY_PARTS)},
                "tour_type": {"type": "string", "enum": list(TOUR_KINDS)},
                "notes": _STR,
            },
            "required": ["home_slug", "full_name", "phone", "preferred_date"],
        },
    },
    "save_caller_details": {
        "handler": save_caller_details,
        "description": (
            "Save the caller as a lead with a summary of the call. Call this near the end of every "
            "call where the caller gave a name or contact detail. Set needs_human_followup when "
            "the caller asked for a person or you could not answer them."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "full_name": _STR,
                "phone": _STR,
                "email": _STR,
                "summary": {**_STR, "description": "Two or three sentences: what they want and what you did."},
                "preferred_location": _STR,
                "max_monthly_dollars": {"type": "number", "minimum": 0},
                "move_in_timeline": {
                    "type": "string",
                    "enum": ["ASAP", "1_3_MONTHS", "3_6_MONTHS", "6_PLUS", "JUST_BROWSING"],
                },
                "has_voucher": _BOOL,
                "has_pets": _BOOL,
                "needs_human_followup": _BOOL,
            },
            "required": ["summary"],
        },
    },
    "check_application_status": {
        "handler": check_application_status,
        "description": (
            "Tell an applicant what stage their application is at. Needs BOTH the email and the "
            "phone number on the application. Returns the stage only; never guess or add detail."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"email": _STR, "phone": _STR},
            "required": ["email", "phone"],
        },
    },
    "get_leasing_policies": {
        "handler": get_leasing_policies,
        "description": "Application fee, lease admin fee, decision timeline, and where to apply.",
        "inputSchema": {"type": "object", "properties": {}},
    },
}
