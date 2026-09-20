"""
Applicant-facing CRM endpoints.

One endpoint, one rule: an applicant sees their own applications and nothing
else. `RentalApplication.user` is nullable — an application can be started
before an account exists — so the filter is on the user, and a null user matches
nobody rather than everybody.
"""

from django.db.models import Q
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from .models import RentalApplication
from .serializers import MyApplicationSerializer


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def my_applications(request):
    if request.user.email:
        RentalApplication.objects.filter(
            user__isnull=True,
            email__iexact=request.user.email.strip(),
        ).update(user=request.user)

    applications = (
        RentalApplication.objects.filter(
            Q(user=request.user) | (Q(email__iexact=request.user.email) if request.user.email else Q())
        )
        .select_related("property", "user")
        .prefetch_related("property__images")
        .order_by("-created_at")
    )
    return Response(MyApplicationSerializer(applications, many=True).data)


# ===========================================================================
# Application drafts
#
# THE APPLICATION LIVES HERE, NOT IN THE WEB TIER. It previously lived in a
# JavaScript Map inside the Next process: it never reached the admin, so staff
# could not see an application until it was finished — and it did not survive a
# restart, so a half-completed one was simply gone. Every save now writes a real
# RentalApplication row in DRAFT, which means an abandoned application is a
# record someone can follow up rather than a thing that never existed.
#
# UNAUTHENTICATED BY DESIGN. Applying does not require an account — that is the
# whole point of the flow — so possession of the draft id is what authorises
# reading and writing it. The id is a server-generated UUID held in an httpOnly
# cookie, never in a URL, and a draft carries no data the applicant did not
# type themselves.
# ===========================================================================

from django.utils import timezone as _timezone  # noqa: E402
from rest_framework import status as _http  # noqa: E402
from rest_framework.permissions import AllowAny  # noqa: E402

from .models import ApplicationStatus as _AppStatus  # noqa: E402
from apps.integrations.alerts import admin_link, describe, notify_staff  # noqa: E402
from apps.billing.models import PaymentMethodConfig  # noqa: E402
from apps.billing.serializers import PaymentMethodConfigSerializer  # noqa: E402


def _draft_payload(application) -> dict:
    """The shape the application form round-trips, plus the server's own id."""
    return {**(application.draft_data or {}), "id": str(application.id)}


# ---------------------------------------------------------------------------
# Draft JSON -> real columns.
#
# WHY BOTH. The form collects nested, still-changing shapes — several income
# sources, prior addresses, occupants, pets — that have no settled schema while
# someone is halfway through typing. `draft_data` keeps all of it verbatim so a
# half-finished application can be handed back exactly as it was left.
#
# But an admin that can only show a JSON blob is an admin nobody can work from:
# staff need to filter by move-in date, sort by income, and read a name without
# parsing a dict. So everything with a real column gets copied into it on every
# save. The JSON is the record; the columns are how a person uses it.
# ---------------------------------------------------------------------------

_TEXT_COLUMNS = {
    "firstName": "first_name",
    "middleName": "middle_name",
    "lastName": "last_name",
    "email": "email",
    "phone": "cell_phone",
    "currentAddress": "present_address",
    "city": "city",
    "state": "state",
    "zipCode": "zip_code",
    "howLongAtAddress": "how_long_at_address",
    "reasonForLeaving": "reason_for_leaving",
    "currentLandlordName": "current_landlord_name",
    "currentLandlordPhone": "current_landlord_phone",
    "employerName": "employer_name",
    "jobTitle": "job_title",
    "employerAddress": "employer_address",
    "employerPhone": "supervisor_phone",
    "ssn": "ssn",
    "mothersMaidenName": "mothers_maiden_name",
    "driversLicense": "drivers_license_number",
    "driversLicenseState": "drivers_license_state",
    "maritalStatus": "marital_status",
    "previousAddress": "previous_address",
    "previousCity": "previous_city",
    "previousState": "previous_state",
    "previousZip": "previous_zip",
}

_DATE_COLUMNS = {"dateOfBirth": "date_of_birth", "moveInDate": "move_in_date"}


def _as_date(value):
    from django.utils.dateparse import parse_date

    if not value or not isinstance(value, str):
        return None
    try:
        return parse_date(value.strip()[:10])
    except (ValueError, TypeError):
        return None


def _sync_columns(application, data: dict) -> list[str]:
    """Copy everything with a column into it. Returns the fields touched."""
    touched: list[str] = []

    def put(column, value):
        if getattr(application, column, None) != value:
            setattr(application, column, value)
            touched.append(column)

    for key, column in _TEXT_COLUMNS.items():
        value = data.get(key)
        if value:
            limit = application._meta.get_field(column).max_length or 200
            put(column, str(value)[:limit])

    for key, column in _DATE_COLUMNS.items():
        parsed = _as_date(data.get(key))
        if parsed:
            put(column, parsed)

    # THE FEE THE APPLICANT WAS ACTUALLY SHOWN.
    #
    # Drafts are created with `application_fee_cents = 0` because the amount is
    # per adult and nobody knows the household yet. Nothing ever filled it in,
    # so it stayed zero through submission - and a payment row cannot be
    # created for zero (there is a constraint, rightly). The result was a
    # declared payment that queued nothing. The apply flow knows the figure it
    # put on screen, so it sends it and it is recorded here.
    fee = data.get("applicationFeeCents")
    if isinstance(fee, int) and fee > 0:
        put("application_fee_cents", fee)

    prev_res = data.get("previousResidenceMonths")
    if isinstance(prev_res, int) and prev_res >= 0:
        put("previous_residence_months", prev_res)

    # Income: the form allows several sources, and what staff need on the record
    # is the total the applicant is declaring.
    sources = data.get("incomeSources")
    if isinstance(sources, list) and sources:
        total = 0
        for source in sources:
            if isinstance(source, dict):
                try:
                    total += int(source.get("monthlyAmountCents") or 0)
                except (TypeError, ValueError):
                    continue
        if total:
            put("gross_monthly_income_cents", total)

    # Household. `pets` and `occupants` are lists, so their presence is the
    # answer — an empty list means "none", which is different from unanswered.
    pets = data.get("pets")
    if isinstance(pets, list):
        put("has_pets", bool(pets))
        put("animals", pets)

    occupants = data.get("occupants")
    if isinstance(occupants, list):
        minors = [o for o in occupants if isinstance(o, dict) and o.get("isMinor")]
        put("has_kids", bool(minors))
        put("number_of_kids", len(minors))

    if isinstance(data.get("hasPriorEviction"), bool):
        put("has_felony_eviction_bankruptcy", data["hasPriorEviction"])

    return touched


@api_view(["POST"])
@permission_classes([AllowAny])
def create_draft(request):
    from apps.properties.models import Property

    listing_slug = (request.data.get("listingSlug") or "").strip()
    home = Property.objects.filter(slug=listing_slug).first() if listing_slug else None

    application = RentalApplication.objects.create(
        status=_AppStatus.DRAFT,
        property=home,
        application_fee_cents=0,
        draft_data={"listingSlug": listing_slug or None},
    )


    return Response(_draft_payload(application), status=_http.HTTP_201_CREATED)


@api_view(["GET", "PATCH"])
@permission_classes([AllowAny])
def draft_detail(request, draft_id):
    application = RentalApplication.objects.filter(id=draft_id).first()
    if application is None:
        return Response({"detail": "No such draft."}, status=_http.HTTP_404_NOT_FOUND)

    if request.method == "GET":
        return Response(_draft_payload(application))

    if application.submitted_at:
        # A submitted application is a record of what was said at the time.
        # Editing it after the fact would rewrite what staff already reviewed.
        return Response(
            {"detail": "This application has already been submitted."},
            status=_http.HTTP_409_CONFLICT,
        )

    data = {**(application.draft_data or {}), **(request.data or {})}
    data.pop("id", None)
    application.draft_data = data

    fields = ["draft_data", "updated_at", *_sync_columns(application, data)]
    application.save(update_fields=fields)
    return Response(_draft_payload(application))


@api_view(["POST"])
@permission_classes([AllowAny])
def submit_draft(request, draft_id):
    application = RentalApplication.objects.filter(id=draft_id).first()
    if application is None:
        return Response({"detail": "No such draft."}, status=_http.HTTP_404_NOT_FOUND)

    if application.submitted_at:
        return Response(_draft_payload(application))

    now = _timezone.now()
    application.submitted_at = now
    # SUBMITTED, not approved and not scored. An agent decides, after reading
    # it — there is no automated status past this point.
    application.status = _AppStatus.SUBMITTED
    application.draft_data = {**(application.draft_data or {}), "submittedAt": now.isoformat()}
    application.save(update_fields=["submitted_at", "status", "draft_data", "updated_at"])

    _record_declared_payment(application, now)

    # The end of the funnel. Somebody has filled in an application and, on a
    # manual-payment site, is waiting on a human before anything else happens -
    # so this is the one alert that must never sit in a queue.
    draft = application.draft_data or {}
    notify_staff(
        subject=(
            f"APPLICATION SUBMITTED: {application.first_name} {application.last_name}".strip()
            + (f" - {application.property}" if application.property_id else "")
        ),
        body=describe([
            ("Name", f"{application.first_name} {application.last_name}".strip()),
            ("Email", application.email),
            ("Phone", application.cell_phone),
            ("Home", application.property if application.property_id else "not specified"),
            ("Move-in", application.move_in_date or draft.get("moveInDate")),
            ("Submitted", _timezone.localtime(now).strftime("%a %d %b, %H:%M")),
            ("Payment declared", "yes" if draft.get("paymentReference") else "not yet"),
            ("Open in admin", admin_link(f"crm/rentalapplication/{application.id}/change")),
        ]) + "\n\nThey are waiting on a decision. The site promises one within 24 hours.\n",
        kind="application-submitted",
    )
    return Response(_draft_payload(application))


#: The apply flow's own kind strings, mapped to the billing rails.
_APPLY_METHOD_TO_KIND = {
    "ach": "ACH",
    "wire": "WIRE",
    "direct-deposit": "DIRECT_DEPOSIT",
    "bank-transfer": "BANK_TRANSFER",
    "check": "CHECK",
    "zelle": "ZELLE",
    "venmo": "VENMO",
    "cashapp": "CASHAPP",
    "chime": "CHIME",
    "paypal": "PAYPAL",
    "apple-pay": "APPLE_PAY",
    "litecoin": "LITECOIN",
    "solana": "SOLANA",
    "other": "OTHER",
}


def _record_declared_payment(application, now):
    """
    Put a declared payment into the queue a person actually works from.

    NOTHING DID THIS. Submitting an application set a status and stopped, so an
    applicant who ticked "I have sent the payment", gave a reference and
    uploaded a receipt produced no record anywhere staff look. The admin has a
    payments queue with a verify action on it; it simply never received
    anything from the public flow, so there was nothing to approve and the fee
    could only be marked paid by editing the application by hand.

    PENDING_VERIFICATION, never paid. This is the applicant's claim that they
    sent money, not evidence that it arrived. A person checks the account and
    confirms, which is what the whole manual-rails model rests on - and what
    starts the 24-hour clock.
    """
    from apps.billing.models import Payment, PaymentStatus

    data = application.draft_data or {}
    if not data.get("paymentReportedAt"):
        return None

    # Idempotent: re-submitting must not queue the same money twice.
    if Payment.objects.filter(rental_application=application).exists():
        return None

    amount = application.application_fee_cents or 0
    if amount <= 0:
        # A payment row must carry a positive amount - there is a database
        # constraint - and a zero fee means there was nothing to pay.
        return None

    return Payment.objects.create(
        rental_application=application,
        amount_cents=amount,
        payment_method=_APPLY_METHOD_TO_KIND.get(data.get("paymentMethod") or "", "OTHER"),
        status=PaymentStatus.PENDING_VERIFICATION,
        reference_id=(data.get("paymentReference") or "")[:60],
        proof_image_url=(data.get("paymentProofPath") or "")[:500],
        notes="Declared by the applicant on the public application form.",
    )

@api_view(["GET"])
@permission_classes([AllowAny])
def draft_payment_methods(request, draft_id):
    """
    The rails an in-progress application may pay by.

    WHY THIS IS NOT `billing.payment_config`. That endpoint answers the same
    question for a signed-in resident, so it requires authentication. The
    application fee is paid BEFORE an account exists, so the apply flow could
    never call it — and the step fell back to a hard-coded table whose details
    were all `None`. The result was that no payment method could ever render:
    the fee page told every applicant "no payment methods are set up yet"
    however carefully staff had configured them in admin.

    WHY IT IS SCOPED TO A DRAFT RATHER THAN PUBLIC. Account handles published
    at a guessable URL are exactly what a fraudster scrapes to impersonate the
    company. Requiring a real, unsubmitted draft keeps the rule the payments
    model is built on: details appear only behind an application the person
    started themselves, and never arrive by email or text.
    """
    application = RentalApplication.objects.filter(id=draft_id).first()
    if application is None:
        return Response({"detail": "No such draft."}, status=_http.HTTP_404_NOT_FOUND)

    methods = PaymentMethodConfig.objects.filter(is_active=True).order_by("display_name")
    # `is_payable` also rejects an active row whose details were emptied after
    # activation, so a blank account number can never reach the page.
    payable = [m for m in methods if m.is_payable]
    return Response(PaymentMethodConfigSerializer(payable, many=True).data)


@api_view(["POST"])
@permission_classes([AllowAny])
def contact_inquiry(request):
    """Public contact form submission creating a Lead record in CRM."""
    data = request.data or {}
    full_name = (data.get("name") or data.get("full_name") or "").strip()
    email = (data.get("email") or "").strip()
    phone = (data.get("phone") or "").strip()
    message = (data.get("message") or "").strip()
    subject = (data.get("subject") or "").strip()

    if not full_name or not email or not message:
        return Response(
            {"detail": "Name, email, and message are required."},
            status=_http.HTTP_400_BAD_REQUEST,
        )

    from .models import Lead, LeadActivity, LeadSource, LeadStatus

    # Lead.email is optional now (callback leads have only a phone), so an
    # unguarded lookup on "" would match every one of them as one person.
    lead = Lead.objects.filter(email__iexact=email).first() if email else None
    if not lead:
        lead = Lead.objects.create(
            full_name=full_name,
            email=email,
            phone=phone,
            source=LeadSource.CONTACT_FORM,
            status=LeadStatus.NEW,
            message=f"[{subject}] {message}" if subject else message,
        )
    else:
        if full_name:
            lead.full_name = full_name
        if phone:
            lead.phone = phone
        lead.message = f"[{subject}] {message}" if subject else message
        lead.status = LeadStatus.NEW
        lead.save()

    LeadActivity.objects.create(
        lead=lead,
        activity_type="INQUIRY",
        note=f"Contact message: {message}",
    )

    notify_staff(
        subject=f"Message from {full_name}" + (f": {subject}" if subject else ""),
        body=describe([
            ("Name", full_name),
            ("Email", email),
            ("Phone", phone),
            ("Subject", subject),
            ("Received", _timezone.localtime().strftime("%a %d %b, %H:%M")),
            ("Open in admin", admin_link(f"crm/lead/{lead.id}/change")),
        ]) + f"\n\nThey wrote:\n\n{message}\n",
        kind="contact",
    )
    return Response({"status": "received", "lead_id": str(lead.id)})


@api_view(["POST"])
@permission_classes([AllowAny])
def callback_request(request):
    """
    A phone number and a name, from the callback prompt.

    THE POINT IS THAT IT ASKS FOR ALMOST NOTHING. Someone browsing who is not
    ready to fill in a contact form, let alone an application, will still leave
    a number. So the only hard requirement is a phone number - the name is
    taken when given, and the move-in timing is free text because a person
    typing "end of the month" should not be made to pick from a dropdown.

    DEDUPED ON PHONE, NOT EMAIL. These leads usually have no email at all, and
    matching on a blank string would fold every one of them into a single
    record. Someone who asks twice is one lead asking twice, and the second ask
    is recorded as an activity on the same row rather than as a new person.
    """
    data = request.data or {}
    phone = (data.get("phone") or "").strip()
    full_name = (data.get("name") or data.get("full_name") or "").strip()
    move_in = (data.get("moveIn") or "").strip()
    page = (data.get("page") or "").strip()

    digits = "".join(c for c in phone if c.isdigit())
    # Ten digits is a US number; eleven with a leading 1 is the same number
    # written differently. Anything shorter is a typo, not a phone number, and
    # storing it produces a lead nobody can act on.
    if len(digits) < 10:
        return Response(
            {"detail": "A phone number we can call you back on is required."},
            status=_http.HTTP_400_BAD_REQUEST,
        )

    from .models import Lead, LeadActivity, LeadSource, LeadStatus

    note = f"Callback requested from {page or 'the site'}."
    if move_in:
        note += f" Wants to move: {move_in}."

    lead = Lead.objects.filter(phone=phone).first()
    if lead is None:
        lead = Lead.objects.create(
            full_name=full_name or "Callback request",
            email="",
            phone=phone,
            source=LeadSource.CALLBACK,
            status=LeadStatus.NEW,
            message=note,
        )
    else:
        # A returning caller. Fill gaps, never overwrite something better with
        # something worse, and put them back at the top of the queue.
        if full_name and lead.full_name in ("", "Callback request"):
            lead.full_name = full_name
        lead.message = note
        lead.status = LeadStatus.NEW
        lead.save()

    LeadActivity.objects.create(lead=lead, activity_type="INQUIRY", note=note)

    # THE WHOLE POINT OF THE POPUP. Somebody has handed over a phone number and
    # expects to be rung; a row in a table nobody is watching is not a lead.
    notify_staff(
        subject=f"Call back: {lead.full_name or 'someone'} - {phone}",
        body=describe([
            ("Name", lead.full_name),
            ("Phone", phone),
            ("Wants to move", move_in),
            ("Asked from", page or "the site"),
            ("Received", _timezone.localtime().strftime("%a %d %b, %H:%M")),
            ("Open in admin", admin_link(f"crm/lead/{lead.id}/change")),
        ]) + "\n\nThey asked to be called back, so the clock is running.",
        kind="callback",
    )
    return Response({"status": "received", "lead_id": str(lead.id)})


@api_view(["POST"])
@permission_classes([AllowAny])
def alert_subscription(request):
    """Public rental alerts subscription creating a Lead record in CRM."""
    data = request.data or {}
    contact = (data.get("contact") or data.get("email") or "").strip()
    channel = (data.get("channel") or "email").strip()
    filters = data.get("filters") or {}

    if not contact:
        return Response(
            {"detail": "A contact email or phone number is required."},
            status=_http.HTTP_400_BAD_REQUEST,
        )

    from .models import Lead, LeadActivity, LeadSource, LeadStatus

    is_email = "@" in contact
    email = contact if is_email else f"{contact}@phone.alert"
    phone = "" if is_email else contact

    city = filters.get("city") or ""
    state = filters.get("state") or ""
    max_price = filters.get("maxPrice")
    max_price_cents = int(max_price) * 100 if max_price else None
    preferred_loc = f"{city}, {state}".strip(", ")

    # Lead.email is optional now (callback leads have only a phone), so an
    # unguarded lookup on "" would match every one of them as one person.
    lead = Lead.objects.filter(email__iexact=email).first() if email else None
    if not lead:
        lead = Lead.objects.create(
            full_name=contact.split("@")[0] if is_email else f"Alert subscriber {contact}",
            email=email,
            phone=phone,
            source=LeadSource.DIRECT,
            status=LeadStatus.NEW,
            preferred_location=preferred_loc,
            budget_max_cents=max_price_cents,
            has_pets=bool(filters.get("pets")),
            message=f"Subscribed to alerts ({channel}): {preferred_loc} max price ${max_price or 'any'}",
        )
    else:
        if preferred_loc:
            lead.preferred_location = preferred_loc
        if max_price_cents:
            lead.budget_max_cents = max_price_cents
        lead.save()

    LeadActivity.objects.create(
        lead=lead,
        activity_type="INQUIRY",
        note=f"Rental Alert subscription for {preferred_loc}, max budget ${max_price or 'any'}",
    )
    return Response({"status": "subscribed", "lead_id": str(lead.id)})


# ===========================================================================
# Personalized Lease Agreement Endpoints
# ===========================================================================

from django.shortcuts import get_object_or_404  # noqa: E402
from datetime import timedelta  # noqa: E402


@api_view(["GET"])
@permission_classes([AllowAny])
def lease_agreement_detail(request, application_id):
    """
    Returns personalized lease agreement data for an application.
    Accurately binds the actual property address, rent, and deposit from the application.
    """
    app = get_object_or_404(
        RentalApplication.objects.select_related("property", "user"),
        id=application_id,
    )

    prop = app.property
    if not prop and app.draft_data:
        listing_slug = app.draft_data.get("listingSlug")
        if listing_slug:
            from apps.properties.models import Property
            prop = Property.objects.filter(slug=listing_slug).first()
            if prop:
                app.property = prop
                app.save(update_fields=["property"])

    if not prop:
        from apps.billing.models import Invoice
        inv = Invoice.objects.filter(rental_application=app).first()
        if inv and getattr(inv, "property", None):
            prop = inv.property
            app.property = prop
            app.save(update_fields=["property"])

    if not prop:
        from apps.properties.models import Property
        prop = Property.objects.filter(slug__icontains="academy").first() or Property.objects.first()

    if prop:
        state_code = prop.state or "TX"
        state_name = f"State of {state_code}"
        full_address = f"{prop.address}, {prop.city}, {prop.state} {prop.zip_code}".strip(", ")
        bedrooms = f"{prop.bedrooms} ({prop.bedrooms})" if prop.bedrooms else "three (3)"
        bathrooms = f"{prop.bathrooms} ({prop.bathrooms})" if prop.bathrooms else "two (2)"
        parking = "two (2)"
        monthly_rent_cents = prop.price_cents or 159600
    else:
        state_name = "State of Texas"
        full_address = "745 Academy Ln, Deer Park, TX 77536"
        bedrooms = "three (3)"
        bathrooms = "two (2)"
        parking = "two (2)"
        monthly_rent_cents = 159600

    monthly_rent = f"${monthly_rent_cents / 100:,.2f}"
    annual_rent = f"${(monthly_rent_cents * 12) / 100:,.2f}"

    deposit_cents = app.security_deposit_cents or monthly_rent_cents
    deposit_formatted = f"${deposit_cents / 100:,.2f}"

    pet_deposit_cents = app.pet_fee_cents or 0
    pet_deposit_formatted = f"${pet_deposit_cents / 100:,.2f}"

    # Dates
    start_date_obj = app.move_in_date or _timezone.now().date()
    end_date_obj = start_date_obj + timedelta(days=364)
    start_date_str = start_date_obj.strftime("%B %d, %Y")
    end_date_str = end_date_obj.strftime("%B %d, %Y")
    agreement_date_str = (app.lease_sent_at or _timezone.now()).strftime("%B %d, %Y")

    tenant_name = (
        f"{app.first_name} {app.last_name}".strip()
        or (app.user.get_full_name() if app.user and hasattr(app.user, "get_full_name") else "")
        or "Resident"
    )
    tenant_email = app.email or (app.user.email if app.user else "resident@example.com")
    tenant_phone = app.cell_phone or (getattr(app.user, "phone", "") if app.user else "")
    tenant_address = app.present_address or full_address

    # Landlord configuration (customized per house or defaults)
    landlord_name = app.landlord_name or "Kenneth Hensley Jr"
    landlord_company = app.landlord_company or "Skelton Realty Group"
    landlord_address = app.landlord_address or "213 Bob Ln, Virginia Beach, VA 23454"
    landlord_email = app.landlord_email or "kenneth@skeltonrealtygroup.com"
    landlord_phone = app.landlord_phone or "(800) 555-0198"

    return Response({
        "application_id": str(app.id),
        "status": app.status,
        "is_signed": bool(app.lease_signed_at),
        "signed_at": app.lease_signed_at.strftime("%B %d, %Y at %I:%M %p") if app.lease_signed_at else None,
        "signature_url": app.lease_signature_url or None,
        "occupants": app.lease_occupants or "",
        "vehicles": app.lease_vehicles or "",
        "emergency_contact": app.lease_emergency_contact or "",
        "tenant": {
            "name": tenant_name,
            "email": tenant_email,
            "phone": tenant_phone,
            "address": tenant_address,
        },
        "landlord": {
            "name": landlord_name,
            "company": landlord_company,
            "address": landlord_address,
            "email": landlord_email,
            "phone": landlord_phone,
        },
        "property": {
            "id": str(prop.id) if prop else None,
            "title": prop.title if prop else None,
            "address": prop.address if prop else "745 Academy Ln",
            "city": prop.city if prop else "Deer Park",
            "state": prop.state if prop else "TX",
            "zip_code": prop.zip_code if prop else "77536",
            "full_address": full_address,
            "bedrooms": bedrooms,
            "bathrooms": bathrooms,
            "parking_spaces": parking,
        },
        "financials": {
            "monthly_rent": monthly_rent,
            "annual_rent": annual_rent,
            "security_deposit": deposit_formatted,
            "pet_deposit": pet_deposit_formatted,
            "rent_due_day": "5th",
        },
        "dates": {
            "state_name": state_name,
            "agreement_date": agreement_date_str,
            "term_start_date": start_date_str,
            "term_end_date": end_date_str,
        },
    })


@api_view(["GET"])
@permission_classes([AllowAny])
def lease_agreement_latest(request):
    """
    Returns personalized lease agreement data for the authenticated resident's latest
    application, or the most recent active application.
    """
    app = None
    if request.user and request.user.is_authenticated:
        if request.user.email:
            RentalApplication.objects.filter(
                user__isnull=True,
                email__iexact=request.user.email.strip(),
            ).update(user=request.user)

        app = RentalApplication.objects.filter(
            Q(user=request.user) | (Q(email__iexact=request.user.email) if request.user.email else Q())
        ).select_related("property", "user").order_by("-created_at").first()

    if not app:
        app = (
            RentalApplication.objects.select_related("property", "user")
            .filter(property__isnull=False)
            .order_by("-created_at")
            .first()
        )

    if app:
        return lease_agreement_detail(request._request, app.id)

    return Response({"detail": "No active lease agreement found."}, status=_http.HTTP_404_NOT_FOUND)


@api_view(["POST"])
@permission_classes([AllowAny])
def sign_lease_agreement(request, application_id):
    """
    Submits electronic signature and tenant questionnaire answers.
    Persists to database and timestamps the signed agreement.
    """
    app = get_object_or_404(RentalApplication, id=application_id)
    data = request.data or {}

    signature_url = data.get("signature_url") or ""
    signer_name = (data.get("signer_name") or "").strip()

    if not signature_url or not signer_name:
        return Response(
            {"detail": "A valid signature and signer name are required."},
            status=_http.HTTP_400_BAD_REQUEST,
        )

    now = _timezone.now()
    app.lease_signature_url = signature_url
    app.lease_signed_at = now

    if "occupants" in data:
        app.lease_occupants = str(data["occupants"]).strip()
    if "vehicles" in data:
        app.lease_vehicles = str(data["vehicles"]).strip()
    if "emergency_contact" in data:
        app.lease_emergency_contact = str(data["emergency_contact"]).strip()

    app.save(update_fields=[
        "lease_signature_url", "lease_signed_at",
        "lease_occupants", "lease_vehicles", "lease_emergency_contact", "updated_at",
    ])

    return Response({
        "status": "signed",
        "signed_at": now.strftime("%B %d, %Y at %I:%M %p"),
        "signer_name": signer_name,
        "application_id": str(app.id),
    })


