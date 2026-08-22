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

from apps.crm.models import Lead, LeadSource
from apps.properties.models import Property

from .models import TourRequest


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
        full_name=name[:200],
        email=email,
        phone=(data.get("phone") or "")[:20],
        preferred_date=preferred_date,
        preferred_time=(data.get("preferredTime") or "")[:20],
        tour_type=(data.get("kind") or "self-tour")[:20],
        notes=(data.get("note") or "")[:2000],
    )

    # `public_id`, never the primary key: this goes back to the browser, and an
    # internal id in a URL invites guessing at other people's.
    return Response({"id": str(tour.public_id), "status": tour.status}, status=http.HTTP_201_CREATED)
