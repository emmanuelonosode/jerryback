"""
Tour requests from the public site.

WHY THIS EXISTS. The site's tour form validated its input, showed "Request
received — a person will confirm within 24 hours", and then dropped it: nothing
was ever sent anywhere. Someone asking to see a house got a promise and no
record, and the admin's "Tour IDs to review" queue could never fill because
nothing wrote to it.

PUBLIC, because asking to view a home does not require an account — that is the
point. Throttled, and it creates a Lead alongside the request so a tour is a
person in the pipeline rather than an orphan row.
"""

from django.utils.dateparse import parse_date
from rest_framework import status as http
from rest_framework.decorators import api_view, permission_classes, throttle_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle

from django.utils import timezone

from apps.crm.models import Lead, LeadSource
from apps.integrations.alerts import (
    admin_link,
    deliver_in_background,
    describe,
    notify_staff,
)
from apps.integrations.models import queue_email
from apps.properties.models import Property

from .models import TourRequest, TourStatus
from .storage import RejectedUpload, store_tour_id

# The response time the public form promises. Kept here so the confirmation
# email and the staff alert cannot quote a different number from the page the
# person just filled in - the first draft of this said 24 hours while the form
# said 4, which is the kind of mismatch that turns a kept promise into a
# broken one.
RESPONSE_HOURS = 4


class TourThrottle(ScopedRateThrottle):
    scope = "tour"


@api_view(["POST"])
@permission_classes([AllowAny])
@throttle_classes([TourThrottle])
def request_tour(request):
    data = request.data or {}

    name = (data.get("name") or "").strip()
    email = (data.get("email") or "").strip()
    preferred_date = parse_date((data.get("preferredDate") or "").strip()) if data.get("preferredDate") else None

    if not name or not email or preferred_date is None:
        return Response(
            {"detail": "Name, email and a preferred date are required."},
            status=http.HTTP_400_BAD_REQUEST,
        )

    slug = (data.get("listingSlug") or "").strip()
    home = Property.objects.public().filter(slug=slug).first() if slug else None

    # A tour request IS a lead. Creating one here means the pipeline reflects
    # everyone who asked to see a home, not only those who filled in a separate
    # enquiry form.
    lead, _ = Lead.objects.get_or_create(
        email=email,
        defaults={"full_name": name, "source": LeadSource.PROPERTY_INQUIRY},
    )

    tour = TourRequest.objects.create(
        lead=lead,
        property=home,
        # PENDING_REVIEW, NOT AWAITING_ID.
        #
        # The model defaults to AWAITING_ID, and there is no ID upload endpoint
        # anywhere in this app - `urls.py` exposes exactly one route, this one.
        # So every public tour request landed in a state the product gives
        # nobody a way to leave: all five on the system sat there, the oldest
        # four days old, looking to staff like the visitor had failed to do
        # something when the visitor had never been asked for anything.
        #
        # What actually happens to a tour request is that a person reads it and
        # confirms a time, which is what the form promises and what this status
        # means. The id_front_url / id_back_url fields stay on the model for
        # staff who check ID at the door, which is how a self-guided viewing
        # works anyway.
        status=TourStatus.PENDING_REVIEW,
        full_name=name[:200],
        email=email,
        phone=(data.get("phone") or "")[:20],
        preferred_date=preferred_date,
        preferred_time=(data.get("preferredTime") or "")[:20],
        tour_type=(data.get("kind") or "self-tour")[:20],
        notes=(data.get("note") or "")[:2000],
    )

    when = tour.preferred_date.strftime("%a %d %b")
    if tour.preferred_time:
        when += f", {tour.preferred_time}"
    home_label = str(home) if home else "a home (not specified)"

    # NOBODY WAS TOLD, ON EITHER SIDE.
    #
    # Five tour requests were sitting in the database, the oldest from four
    # days earlier, every one of them at AWAITING_ID. Staff got no alert. The
    # person who asked got no email. The form said "a person will confirm
    # within 24 hours" and then nothing happened to anybody, which is exactly
    # how a real letting business ends up looking like a fake listing.
    notify_staff(
        subject=f"Tour request: {name} - {home_label}",
        body=describe([
            ("Name", name),
            ("Email", email),
            ("Phone", tour.phone),
            ("Home", home_label),
            ("Wants to visit", when),
            ("Type", tour.tour_type),
            ("Notes", tour.notes),
            ("Received", timezone.localtime().strftime("%a %d %b, %H:%M")),
            ("Open in admin", admin_link(f"scheduler/tourrequest/{tour.id}/change")),
        ]) + (
            "\n\nConfirm a time with them. The form they used promises that within "
            f"{RESPONSE_HOURS} business hours, so that is the clock you are on.\n"
        ),
        kind="tour",
    )

    # And the person who asked. A request that vanishes into silence is the
    # single most common reason someone stops trusting a rental site, and it
    # costs one email to not do that.
    # Queued, not sent inline: the person is watching a spinner, and their own
    # confirmation is the one message they are guaranteed to see anyway. It
    # leaves on the same background thread as the staff alert below.
    confirmation = queue_email(
        send_now=False,
        to_email=email,
        subject=f"We have your tour request for {home_label}",
        body_text=(
            f"Hi {name.split(' ')[0] if name else 'there'},\n\n"
            f"Thanks for asking to see {home_label}. Here is what you told us:\n\n"
            f"  When you would like to visit: {when}\n"
            f"  Type of visit: {tour.tour_type}\n\n"
            f"Someone will confirm the time with you within {RESPONSE_HOURS} business "
            "hours. If your plans "
            "change before then, just reply to this email and we will move it - "
            "there is nothing to cancel and nothing to pay.\n\n"
            "Any questions at all, just reply to this email - a person reads it.\n"
        ),
        template="tour-received",
    )
    if confirmation is not None:
        deliver_in_background([confirmation])

    # `public_id`, never the primary key: this goes back to the browser, and an
    # internal id in a URL invites guessing at other people's.
    return Response({"id": str(tour.public_id), "status": tour.status}, status=http.HTTP_201_CREATED)


@api_view(["POST"])
@permission_classes([AllowAny])
@throttle_classes([TourThrottle])
def upload_tour_id(request, public_id):
    """
    Attach an ID photo to a tour request.

    AUTHORISED BY `public_id`, WHICH IS WHY IT IS UNGUESSABLE. There is no
    account at this point - somebody asked to see a house thirty seconds ago -
    so the capability to attach a document to one specific request is the
    random id handed back when that request was created, and nothing else.
    The primary key is never exposed for exactly this reason.

    OPTIONAL, AND IT MUST STAY OPTIONAL. An ID speeds up a self-guided
    viewing, and requiring one before we will even talk about a time is how
    the previous version of this flow ended up with five requests stranded in
    an AWAITING_ID state that nothing could leave. The tour is already
    requested by the time this is called; this only ever adds to it.

    ONLY BEFORE REVIEW. Once staff have decided, the documents are on their
    way to being purged and re-uploading into that is pointless.
    """
    tour = TourRequest.objects.filter(public_id=public_id).first()
    if tour is None:
        return Response({"detail": "No such tour request."}, status=http.HTTP_404_NOT_FOUND)
    if tour.id_purged_at is not None:
        return Response(
            {"detail": "This request has already been reviewed."},
            status=http.HTTP_409_CONFLICT,
        )

    saved = []
    for side in ("front", "back"):
        upload = request.FILES.get(f"id{side.capitalize()}") or request.FILES.get(side)
        if upload is None or upload.size == 0:
            continue
        try:
            filename = store_tour_id(upload, tour_public_id=tour.public_id, side=side)
        except RejectedUpload as refusal:
            # Said out loud, with the reason. A silent refusal on a document
            # upload leaves somebody believing they have sent us something.
            return Response({"detail": str(refusal)}, status=http.HTTP_400_BAD_REQUEST)
        setattr(tour, f"id_{side}_url", filename)
        saved.append(side)

    if not saved:
        return Response(
            {"detail": "Attach a photo of your ID."},
            status=http.HTTP_400_BAD_REQUEST,
        )

    tour.save(update_fields=[f"id_{side}_url" for side in saved])

    notify_staff(
        subject=f"ID received for {tour.full_name}'s tour",
        body=describe([
            ("Name", tour.full_name),
            ("Email", tour.email),
            ("Home", str(tour.property) if tour.property_id else "not specified"),
            ("Sides received", ", ".join(saved)),
            ("Open in admin", admin_link(f"scheduler/tourrequest/{tour.id}/change")),
        ]) + "\n\nHeld only until the viewing is reviewed, then deleted automatically.\n",
        kind="tour-id",
    )
    return Response({"received": saved}, status=http.HTTP_200_OK)
