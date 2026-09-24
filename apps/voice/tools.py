"""
What the phone agent is allowed to do.

A CALLER IS AN ANONYMOUS MEMBER OF THE PUBLIC WHO CAN SAY ANYTHING. Every tool
here is reachable by whoever dials the number and talks the model into calling
it, so this list is the same trust level as the public website: the writes the
website already accepts anonymously (a tour booking and a lead), plus changes
to a caller's own tour once they prove it is theirs.
Nothing here reads another person's application, a payment, an ID document,
a door code or a staff record. That is deliberate, and it is why this is a short list rather
than "the whole admin": a prompt-injected voice agent with staff access is a
data breach that anyone can trigger with a phone call.

Each tool returns plain JSON-serialisable data. Money goes out twice - integer
cents, which is the house unit, and a display string, which is what the model
should actually say aloud so it never does arithmetic on a price.
"""

from datetime import date, timedelta

from django.conf import settings
from django.db import transaction
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
from apps.scheduler.models import (
    ACTIVE_VIEWING_STATUSES, TourRequest, TourStatus, Viewing, ViewingStatus,
)
from apps.scheduler.views import RESPONSE_HOURS

from .slots import open_slots, slot_at, zone_for

MAX_RESULTS = 10
# How far ahead a tour can be requested. A date three months out is almost
# always a misheard month, and a person will not hold a slot that long anyway.
TOUR_HORIZON_DAYS = 60
TOUR_KINDS = ("self-tour", "in-person", "video")
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


def _tour_date(raw) -> date:
    when = parse_date(str(raw or ""))
    today = timezone.localdate()
    if when is None:
        raise ToolError("The date must be in YYYY-MM-DD form.")
    if when < today:
        raise ToolError("That date has already passed. Confirm the date with the caller.")
    if when > today + timedelta(days=TOUR_HORIZON_DAYS):
        raise ToolError(f"Tours can be booked up to {TOUR_HORIZON_DAYS} days ahead. Confirm the date with the caller.")
    return when


def _reference(tour) -> str:
    return str(tour.public_id)[:8].upper()


def _id_upload_url(tour) -> str:
    return f"{settings.PUBLIC_SITE_URL.rstrip('/')}/tour-id/{tour.public_id}"


def _when_label(tour) -> str:
    if tour.viewing_id and tour.viewing.status in ACTIVE_VIEWING_STATUSES:
        local = tour.viewing.scheduled_at.astimezone(zone_for(tour.property)) if tour.property_id else tour.viewing.scheduled_at
        return local.strftime("%a %d %b at %I:%M %p %Z").replace(" 0", " ")
    label = tour.preferred_date.strftime("%a %d %b")
    return f"{label}, {tour.preferred_time}" if tour.preferred_time else label


def find_tour_times(args: dict) -> dict:
    home = _find_home(args.get("home_slug") or "")
    if home is None:
        raise ToolError("That home is not on the market. Use search_homes to find its slug first.")
    day = _tour_date(args.get("date"))
    if not home.allow_selfshow:
        return {
            "self_tour_available": False,
            "tell_the_caller": (
                "This home is shown by a member of the team rather than self-guided. "
                "Offer an in-person or video tour request instead."
            ),
        }
    slots = open_slots(home, day)
    return {
        "self_tour_available": True,
        "date": day.isoformat(),
        "timezone": zone_for(home).key,
        "open_times": [{"time": s.value, "say": s.label} for s in slots],
        "tell_the_caller": (
            "Offer two or three of these times, in the home's local time."
            if slots else "No self-guided times are left that day. Try another date."
        ),
    }


def book_tour(args: dict) -> dict:
    name = (args.get("full_name") or "").strip()[:200]
    phone = (args.get("phone") or "").strip()[:20]
    email = (args.get("email") or "").strip()
    if not name:
        raise ToolError("Ask the caller for their name before booking.")
    if len(_digits(phone)) < 10:
        raise ToolError("A ten-digit phone number is required.")
    when = _tour_date(args.get("preferred_date"))

    kind = (args.get("tour_type") or "self-tour").strip().lower()
    if kind not in TOUR_KINDS:
        raise ToolError(f"tour_type must be one of: {', '.join(TOUR_KINDS)}.")

    home = _find_home(args.get("home_slug") or "")
    if home is None:
        raise ToolError("That home is not on the market. Use search_homes to find its slug first.")

    viewing = None
    if kind == "self-tour":
        if not home.allow_selfshow:
            raise ToolError("This home is not set up for self-guided tours. Offer an in-person or video tour instead.")
        with transaction.atomic():
            # Re-checked at the moment of writing, not trusted from the earlier
            # find_tour_times call: someone else may have taken it since.
            slot = slot_at(home, when, (args.get("time") or "").strip())
            if slot is None:
                raise ToolError("That time is not open. Call find_tour_times again and offer the caller another.")
            viewing = Viewing.objects.create(
                property=home, scheduled_at=slot.starts_at, status=ViewingStatus.SCHEDULED,
                notes="Self-guided tour booked by the phone agent. Send the access code only once ID is verified.",
            )
        time_value = slot.value
    else:
        time_value = (args.get("preferred_time") or "").strip().lower()
        if time_value and time_value not in DAY_PARTS:
            raise ToolError(f"preferred_time must be one of: {', '.join(DAY_PARTS)}.")

    lead = _lead_for(phone=phone, email=email, name=name)
    if lead.property_interest_id is None:
        lead.property_interest = home
        lead.save(update_fields=["property_interest", "updated_at"])
    if viewing is not None:
        viewing.lead = lead
        viewing.save(update_fields=["lead"])

    tour = TourRequest.objects.create(
        lead=lead, property=home, viewing=viewing,
        # A person still reviews every tour - for a self-tour, that review is
        # the ID check that releases the door code.
        status=TourStatus.PENDING_REVIEW,
        full_name=name, email=email, phone=phone,
        preferred_date=when, preferred_time=time_value, tour_type=kind,
        notes=("Booked by the phone agent. " + (args.get("notes") or "")).strip()[:2000],
    )
    label = _when_label(tour)
    LeadActivity.objects.create(
        lead=lead, activity_type=ActivityType.VIEWING_BOOKED,
        note=f"Phone agent booked a {kind} tour of {home} for {label}",
    )

    if kind == "self-tour":
        staff_ask = (
            "A self-guided slot is booked and held. Check their ID (they were sent an upload "
            "link if they gave an email; otherwise ask for it), then send the access code."
        )
        tell = (
            f"Your self-guided tour is booked for {label}. Before the tour we need a photo of your "
            "ID; once it is checked, we send you the door code. You will not get a code over this call."
        )
    else:
        staff_ask = f"Confirm a time with them within {RESPONSE_HOURS} business hours, as the caller was promised."
        tell = (
            f"The tour is requested, not yet confirmed. A person will confirm the exact time within "
            f"{RESPONSE_HOURS} business hours."
        )

    notify_staff(
        subject=f"Tour booked by phone ({kind}): {name} - {home}",
        body=describe([
            ("Name", name), ("Phone", phone), ("Email", email),
            ("Home", str(home)), ("When", label), ("Type", kind), ("Reference", _reference(tour)),
            ("Notes", tour.notes),
            ("Open in admin", admin_link(f"scheduler/tourrequest/{tour.id}/change")),
        ]) + f"\n\n{staff_ask}\n",
        kind="tour",
    )
    if email:
        body = (
            f"Hi {name.split(' ')[0]},\n\n"
            f"Thanks for calling about {home}.\n\n"
            f"  Tour: {kind}\n  When: {label}\n  Reference: {_reference(tour)}\n\n"
        )
        if kind == "self-tour":
            body += (
                "One step left: upload a photo of your government ID here, so we can send "
                f"your door code before the tour:\n\n  {_id_upload_url(tour)}\n\n"
                "The ID is deleted after it has been checked.\n"
            )
        else:
            body += f"Someone will confirm the exact time with you within {RESPONSE_HOURS} business hours.\n"
        body += f"\nThe listing: {_listing_url(home)}\nNeed to change it? Call us back or reply to this email.\n"
        confirmation = queue_email(
            send_now=False, to_email=email,
            subject=f"Your tour of {home}", body_text=body, template="tour-received",
        )
        if confirmation is not None:
            deliver_in_background([confirmation])
        if kind == "self-tour":
            tell += " We have emailed you a link to upload your ID."
    elif kind == "self-tour":
        tell += " A member of the team will contact you to collect your ID."

    return {
        "reference": _reference(tour),
        "status": "booked" if kind == "self-tour" else "requested",
        "home": str(home),
        "when": label,
        "tell_the_caller": tell,
    }


def _caller_tours(args: dict) -> list:
    """
    Tours belonging to this caller: the phone on the booking must match, AND
    the caller must know one more thing about it - the reference, the email,
    or the surname. Caller ID alone can be spoofed, and what comes back is
    where and when an empty house will have a stranger at the door.
    """
    phone = _digits(args.get("phone") or "")
    if len(phone) < 10:
        raise ToolError("The phone number the tour was booked with is needed.")
    reference = (args.get("reference") or "").replace("-", "").strip().upper()[:8]
    email = (args.get("email") or "").strip().lower()
    surname = (args.get("last_name") or "").strip().lower()
    if not (reference or email or surname):
        raise ToolError("Also ask for the booking reference, the email used, or their last name.")

    recent = (
        TourRequest.objects.filter(phone__endswith=phone[-4:], created_at__gte=timezone.now() - timedelta(days=120))
        .exclude(status__in=[TourStatus.CANCELLED, TourStatus.REJECTED])
        .select_related("property", "viewing")
    )
    mine = []
    for tour in recent:
        if _digits(tour.phone) != phone:
            continue
        if reference and _reference(tour) != reference:
            continue
        if not reference and email and tour.email.lower() != email:
            continue
        if not reference and not email and tour.full_name.lower().split()[-1:] != [surname]:
            continue
        if tour.viewing_id and tour.viewing.status not in ACTIVE_VIEWING_STATUSES:
            continue
        if tour.preferred_date < timezone.localdate():
            continue
        mine.append(tour)
    return mine


def _one_tour(args: dict):
    tours = _caller_tours(args)
    if not tours:
        raise ToolError("No upcoming tour matches those details. Check the phone number and reference with the caller.")
    if len(tours) > 1 and not args.get("reference"):
        raise ToolError(
            "They have more than one upcoming tour: "
            + "; ".join(f"{_reference(t)} - {t.property} on {_when_label(t)}" for t in tours)
            + ". Ask which one, then pass its reference."
        )
    return tours[0]


def check_my_tours(args: dict) -> dict:
    tours = _caller_tours(args)
    return {
        "tours": [
            {
                "reference": _reference(t),
                "home": str(t.property) if t.property_id else "not specified",
                "type": t.tour_type,
                "when": _when_label(t),
                "status": {
                    TourStatus.AWAITING_ID: "Waiting for their ID",
                    TourStatus.PENDING_REVIEW: "Booked; waiting for our team to check ID and confirm",
                    TourStatus.APPROVED: "Confirmed",
                }.get(t.status, t.get_status_display()),
                "id_received": bool(t.id_front_url or t.id_back_url or t.id_purged_at),
            }
            for t in tours
        ],
        # Never a door code, whatever the caller says. It goes out in writing
        # to the verified contact, not to whoever is on the phone.
        "note": "Door codes are never given over the phone; they are sent after ID is verified.",
    }


def reschedule_tour(args: dict) -> dict:
    tour = _one_tour(args)
    when = _tour_date(args.get("new_date"))
    old_label = _when_label(tour)

    if tour.tour_type == "self-tour" and tour.property_id and tour.property.allow_selfshow:
        with transaction.atomic():
            slot = slot_at(tour.property, when, (args.get("new_time") or "").strip(), exclude_viewing=tour.viewing)
            if slot is None:
                raise ToolError("That time is not open. Call find_tour_times and offer the caller another.")
            # A self-tour booked before slots existed holds no viewing yet.
            viewing = tour.viewing or Viewing(
                property=tour.property, lead_id=tour.lead_id, status=ViewingStatus.SCHEDULED,
                notes="Self-guided tour moved by the phone agent. Send the access code only once ID is verified.",
            )
            viewing.scheduled_at = slot.starts_at
            # The old code was issued for the old window; staff issue a new one.
            viewing.access_code, viewing.access_code_expires_at = "", None
            viewing.reminder_24h_sent = viewing.reminder_2h_sent = viewing.confirmation_sent = False
            viewing.save()
        tour.viewing, tour.preferred_time = viewing, slot.value
    else:
        part = (args.get("new_time") or "").strip().lower()
        if part and part not in DAY_PARTS:
            raise ToolError(f"For this tour, new_time must be one of: {', '.join(DAY_PARTS)}.")
        if tour.viewing_id:
            tour.viewing.status = ViewingStatus.CANCELLED
            tour.viewing.save(update_fields=["status"])
            tour.viewing = None
        tour.preferred_time = part
        tour.status = TourStatus.PENDING_REVIEW
    tour.preferred_date = when
    tour.save(update_fields=["preferred_date", "preferred_time", "viewing", "status"])
    new_label = _when_label(tour)

    if tour.lead_id:
        LeadActivity.objects.create(lead_id=tour.lead_id, activity_type=ActivityType.NOTE,
                                    note=f"Phone agent moved tour {_reference(tour)} from {old_label} to {new_label}")
    notify_staff(
        subject=f"Tour moved by phone: {tour.full_name} - {tour.property}",
        body=describe([
            ("Reference", _reference(tour)), ("Was", old_label), ("Now", new_label), ("Type", tour.tour_type),
            ("Open in admin", admin_link(f"scheduler/tourrequest/{tour.id}/change")),
        ]) + "\n\nAny door code already sent is void; issue a new one for the new time.\n",
        kind="tour",
    )
    return {"reference": _reference(tour), "when": new_label, "tell_the_caller": f"Done - your tour is now {new_label}."}


def cancel_tour(args: dict) -> dict:
    tour = _one_tour(args)
    if tour.viewing_id:
        viewing = tour.viewing
        viewing.status = ViewingStatus.CANCELLED
        viewing.access_code, viewing.access_code_expires_at = "", None
        viewing.save(update_fields=["status", "access_code", "access_code_expires_at"])
    # `reviewed_at` is what starts the ID-deletion clock, so a cancelled
    # tour's documents are purged on the same schedule as a reviewed one.
    tour.status, tour.reviewed_at = TourStatus.CANCELLED, timezone.now()
    tour.save(update_fields=["status", "reviewed_at"])
    if tour.lead_id:
        LeadActivity.objects.create(lead_id=tour.lead_id, activity_type=ActivityType.NOTE,
                                    note=f"Phone agent cancelled tour {_reference(tour)} at the caller's request")
    notify_staff(
        subject=f"Tour cancelled by phone: {tour.full_name} - {tour.property}",
        body=describe([
            ("Reference", _reference(tour)), ("Was", _when_label(tour)),
            ("Open in admin", admin_link(f"scheduler/tourrequest/{tour.id}/change")),
        ]),
        kind="tour",
    )
    return {"cancelled": True, "tell_the_caller": "Your tour is cancelled. You are welcome to book another any time."}


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


# Mirrors frontend lib/content/qualifications.ts (TIER_ONE, INCOME_DOCUMENTS).
# Change them together: the phone line must not state a different rule from
# the published criteria page - that gap is exactly the Fair Housing exposure
# the published criteria exist to close.
WHAT_WE_ASK = [
    "You want the home: apply for any home listed as available. No pre-qualification and no minimum score; a person reads every application.",
    "You can afford the monthly cost: the all-in total shown on the listing, which already includes every required fee.",
    "You agree the terms: lease length and which utilities are yours are set with you.",
    "Identification: a government photo ID. An ITIN is accepted in place of an SSN.",
]
INCOME_DOCUMENTS = [
    "Recent pay stubs",
    "Bank statements showing regular deposits",
    "Tax returns or 1099s, for self-employed and contract income",
    "An offer letter, for a job accepted but not yet started",
    "Benefit award letters, including Social Security, disability, and housing vouchers",
    "Court-ordered support documentation",
]


def get_leasing_policies(_args: dict) -> dict:
    site = settings.PUBLIC_SITE_URL.rstrip("/")
    contact = {
        "phone": settings.COMPANY_PHONE, "phone_hours": settings.COMPANY_PHONE_HOURS,
        "email": settings.COMPANY_EMAIL, "office_address": settings.COMPANY_ADDRESS.replace("|", ", "),
    }
    return {
        # Unset values are left out rather than sent blank, so the model has
        # nothing to fill in with a guess.
        "contact": {k: v for k, v in contact.items() if v},
        "application_fee": format_usd(settings.APPLICATION_FEE_CENTS),
        "lease_admin_fee": format_usd(settings.MOVE_IN_LEASE_ADMIN_FEE_CENTS),
        "application_decision_hours": settings.DECISION_WINDOW_HOURS,
        "what_we_ask_of_applicants": WHAT_WE_ASK,
        "income_documents_accepted": INCOME_DOCUMENTS,
        "housing_vouchers": "Many homes accept vouchers; search with voucher_accepted to find them.",
        "pets": "Set per home; get_home_details gives each home's pet policy and any pet fees.",
        "tours": {
            "self_guided": (
                "Book a time with find_tour_times and book_tour. The caller uploads a photo of their ID "
                "from a link we email; once it is checked, the door code is sent to them. Door codes are "
                "never given over the phone."
            ),
            "with_staff": f"In-person or video tours are requested; staff confirm a time within {RESPONSE_HOURS} business hours.",
            "cost": "Tours are free and do not require an application.",
        },
        "how_to_apply": f"Online at {site}/apply, or from any listing page.",
        "pages": {
            "qualifications": f"{site}/qualifications",
            "fees": f"{site}/fees",
            "housing_vouchers": f"{site}/housing-vouchers",
            "fair_housing": f"{site}/fair-housing",
        },
    }


# --- registry ----------------------------------------------------------------

_STR = {"type": "string"}
_BOOL = {"type": "boolean"}
_TOUR_LOOKUP = {
    "phone": {**_STR, "description": "The phone number the tour was booked with."},
    "reference": {**_STR, "description": "The 8-character booking reference, if they have it."},
    "email": _STR,
    "last_name": _STR,
}

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
    "find_tour_times": {
        "handler": find_tour_times,
        "description": (
            "Open self-guided tour times for one home on one date, in the home's local time. "
            "Call this before booking a self-tour and offer the caller two or three times."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "home_slug": _STR,
                "date": {**_STR, "description": "YYYY-MM-DD."},
            },
            "required": ["home_slug", "date"],
        },
    },
    "book_tour": {
        "handler": book_tour,
        "description": (
            "Book a tour. tour_type 'self-tour' holds an exact time from find_tour_times (pass it as "
            "'time'); the caller then uploads their ID from an emailed link and staff send the door "
            "code. 'in-person' or 'video' is a request for a part of the day that staff confirm. "
            "Read the home, date, time and phone number back to the caller and get a yes first."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "home_slug": {**_STR, "description": "The slug from search_homes or get_home_details."},
                "tour_type": {"type": "string", "enum": list(TOUR_KINDS)},
                "full_name": _STR,
                "phone": {**_STR, "description": "Ten-digit US number. Use the caller's number if they agree."},
                "email": {**_STR, "description": "Strongly recommended for self-tours: the ID upload link is emailed."},
                "preferred_date": {**_STR, "description": "YYYY-MM-DD."},
                "time": {**_STR, "description": "Self-tour only: a 'time' value from find_tour_times, e.g. 14:30."},
                "preferred_time": {"type": "string", "enum": list(DAY_PARTS), "description": "In-person/video only."},
                "notes": {**_STR, "description": "Anything to arrange, e.g. step-free access."},
            },
            "required": ["home_slug", "tour_type", "full_name", "phone", "preferred_date"],
        },
    },
    "check_my_tours": {
        "handler": check_my_tours,
        "description": (
            "Look up a caller's upcoming tours. Needs the phone number the tour was booked with plus "
            "ONE of: reference, email, or last name. Never reveals a door code."
        ),
        "inputSchema": {"type": "object", "properties": _TOUR_LOOKUP, "required": ["phone"]},
    },
    "reschedule_tour": {
        "handler": reschedule_tour,
        "description": (
            "Move a caller's tour. Same identity check as check_my_tours. For a self-tour, new_time "
            "is a value from find_tour_times; otherwise a part of the day. Confirm with the caller first."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                **_TOUR_LOOKUP,
                "new_date": {**_STR, "description": "YYYY-MM-DD."},
                "new_time": _STR,
            },
            "required": ["phone", "new_date"],
        },
    },
    "cancel_tour": {
        "handler": cancel_tour,
        "description": "Cancel a caller's tour. Same identity check as check_my_tours. Confirm with the caller first.",
        "inputSchema": {"type": "object", "properties": _TOUR_LOOKUP, "required": ["phone"]},
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
        "description": (
            "Company contact details and office hours, fees, what we ask of applicants, accepted "
            "income documents, how tours work, and where to apply. Call this for general questions."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
}
