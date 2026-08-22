"""
Saved properties.

DELETE IS SCOPED, NOT LOOKED UP THEN CHECKED. `filter(user=...).delete()` cannot
remove a row that is not the caller's, whereas `get(pk=...)` followed by an
ownership `if` is one forgotten branch away from letting anyone clear anyone
else's saved list. The scoped form makes the wrong version unwriteable.

A delete that matches nothing returns 404 rather than 204. Reporting success for
a row that was never yours tells a prober that a shared id space exists; more
practically, a resident whose list did not change deserves to know why.
"""

from rest_framework import status as http_status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from .models import FavoriteProperty
from .serializers import FavoriteSerializer


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def favorites(request):
    saved = (
        FavoriteProperty.objects.filter(user=request.user)
        .select_related("property")
        .prefetch_related("property__images")
        .order_by("-created_at")
    )
    return Response(FavoriteSerializer(saved, many=True).data)


@api_view(["DELETE"])
@permission_classes([IsAuthenticated])
def remove_favorite(request, favorite_id):
    removed, _ = FavoriteProperty.objects.filter(user=request.user, id=favorite_id).delete()
    if not removed:
        return Response(
            {"detail": "That saved home is not on your list."},
            status=http_status.HTTP_404_NOT_FOUND,
        )
    return Response(status=http_status.HTTP_204_NO_CONTENT)

# ===========================================================================
# Public inventory
#
# UNAUTHENTICATED AND READ-ONLY. This is the catalogue the marketing site
# renders; until it existed the site fell back to generated fixtures, which
# meant editing a home in the admin changed nothing a visitor could see.
#
# `public()` filters to published inventory in a publicly-visible status, so an
# unpublished draft or an off-market home is invisible rather than merely
# unlinked.
# ===========================================================================

import operator  # noqa: E402
from functools import reduce  # noqa: E402
from django.db.models import Case, Count, IntegerField, Q, Value, When  # noqa: E402
from django.utils.dateparse import parse_date  # noqa: E402
from rest_framework.pagination import PageNumberPagination  # noqa: E402
from rest_framework.permissions import AllowAny  # noqa: E402

from .models import Property, normalise_search_text  # noqa: E402
from .serializers import (  # noqa: E402
    MapPinSerializer,
    PublicPropertyDetailSerializer,
    PublicPropertyListSerializer,
)


class InventoryPagination(PageNumberPagination):
    page_size = 24
    page_size_query_param = "page_size"
    # The site asks for 200 in one call to build its sitemap; anything larger is
    # someone scraping the catalogue rather than rendering it.
    max_page_size = 200


def _int_param(request, name):
    raw = request.query_params.get(name)
    if raw in (None, ""):
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None



def _apply_search(queryset, raw_query):
    """
    Free-text search over the address, city, state, ZIP and neighbourhood.

    WHY THIS IS NOT A `city__iexact` FILTER. It used to be, which meant the
    search box only ever answered whole city names: "Verdugos", "5445 Verdugos
    Pl" and "78244" all returned nothing at all, on a site whose main input is
    a search box. A renter searches for the street they were shown or the ZIP
    they were told, not for the exact spelling of a municipality.

    HOW IT MATCHES. The query is normalised the same way the stored haystack
    is, then split into tokens, and EVERY token must appear. AND rather than
    OR because "lake lilburn" should mean both words - an OR search on two
    common tokens returns most of the catalogue and looks broken.

    HOW IT RANKS. Ordering is by how the match was made, not just that it was:
    a haystack that starts with the query is somebody typing an address, and
    that beats a token found in the middle of an unrelated record. Ranking is
    done with database CASE expressions rather than in Python so that it
    survives pagination - sorting a single page is sorting the wrong set.

    WHEN NOTHING MATCHES, it retries with OR and ranks by how many tokens hit.
    That is what rescues one mistyped or extra word ("verdugos crt san
    antonio") instead of returning an empty page.
    """
    query = normalise_search_text(raw_query)
    if not query:
        return queryset, False

    tokens = [t for t in query.split() if t][:8]
    if not tokens:
        return queryset, False

    def ranked(qs):
        return qs.annotate(
            search_rank=Case(
                # Typing the start of an address.
                When(search_text__startswith=query, then=Value(0)),
                # The whole phrase, somewhere in the record.
                When(search_text__contains=query, then=Value(1)),
                # A token at the start of the haystack, i.e. a street number.
                When(search_text__startswith=tokens[0], then=Value(2)),
                default=Value(3),
                output_field=IntegerField(),
            )
        ).order_by("search_rank", "-is_featured", "-created_at", "id")

    strict = queryset
    for token in tokens:
        strict = strict.filter(search_text__contains=token)
    if strict.exists():
        return ranked(strict), True

    # Nothing matched every token. Fall back to any token, most hits first.
    loose = queryset.filter(
        reduce(operator.or_, (Q(search_text__contains=t) for t in tokens))
    ).annotate(
        hits=reduce(
            operator.add,
            (
                Case(When(search_text__contains=t, then=Value(1)), default=Value(0),
                     output_field=IntegerField())
                for t in tokens
            ),
        )
    ).order_by("-hits", "-is_featured", "-created_at", "id")
    return loose, True


_ORDERINGS = {
    "price-asc": ("total_monthly_cents",),
    "price-desc": ("-total_monthly_cents",),
    "newest": ("-created_at",),
    "beds-desc": ("-bedrooms",),
}


@api_view(["GET"])
@permission_classes([AllowAny])
def inventory(request):
    queryset = (
        Property.objects.public()
        .with_total_monthly()
        .prefetch_related("images")
        .order_by("-is_featured", "-created_at")
    )

    # Free text first: it reorders the queryset, and the ordering it applies
    # must survive everything below it.
    queryset, searched = _apply_search(queryset, request.query_params.get("q", ""))

    city = request.query_params.get("city", "").strip()
    if city:
        queryset = queryset.filter(city__iexact=city)

    state = request.query_params.get("state", "").strip()
    if state:
        queryset = queryset.filter(state__iexact=state)

    beds = _int_param(request, "min_bedrooms")
    if beds is not None:
        queryset = queryset.filter(bedrooms__gte=beds)

    # Compared against the ALL-IN total, not base rent. Filtering on price_cents
    # here would show a renter capping at $2,000 a home that costs $2,150 to
    # live in, which is the practice this brand positions against.
    max_price = _int_param(request, "max_price_cents")
    if max_price is not None:
        queryset = queryset.filter(total_monthly_cents__lte=max_price)

    min_price = _int_param(request, "min_price_cents")
    if min_price is not None:
        queryset = queryset.filter(total_monthly_cents__gte=min_price)

    # `bathrooms` is a derived half-bath count, so the comparison happens in the
    # stored unit rather than on the Python property, which the ORM cannot see.
    baths = _int_param(request, "min_bathrooms")
    if baths is not None:
        queryset = queryset.filter(half_bathrooms__gte=baths * 2)

    home_type = request.query_params.get("type", "").strip()
    if home_type:
        queryset = queryset.filter(type__iexact=home_type)

    # A home with no `available_from` is available now, not unknown — excluding
    # nulls here would hide most of the catalogue behind a move-in date.
    available_by = request.query_params.get("available_by", "").strip()
    if available_by:
        parsed = parse_date(available_by)
        if parsed is not None:
            queryset = queryset.filter(
                Q(available_from__lte=parsed) | Q(available_from__isnull=True)
            )

    if request.query_params.get("voucher_accepted") == "true":
        queryset = queryset.filter(voucher_accepted=True)
    if request.query_params.get("pets_allowed") == "true":
        queryset = queryset.filter(pets_allowed=True)

    # Every ordering ends with `id` so that homes tying on the sort key keep a
    # stable order across pages. Without it Postgres may return the same row on
    # page 1 and page 2 and drop another entirely.
    # An explicit sort still wins - somebody who picked "price, lowest first"
    # means it - but relevance is the default whenever a query was typed.
    ordering = _ORDERINGS.get(request.query_params.get("sort", ""))
    if ordering and not (searched and not request.query_params.get("sort")):
        queryset = queryset.order_by(*ordering, "id")

    paginator = InventoryPagination()
    page = paginator.paginate_queryset(queryset, request)
    return paginator.get_paginated_response(
        PublicPropertyListSerializer(page, many=True, context={'request': request}).data,
    )


@api_view(["GET"])
@permission_classes([AllowAny])
def inventory_cities(request):
    """
    Cities with LIVE inventory, for the location hubs and the sitemap.

    `rentable()`, not `public()`: a leased home stays publicly visible at its
    own URL so an inbound link does not 404, but counting it here would tell a
    renter there are homes in a city where there is nothing to rent.
    """
    rows = (
        Property.objects.rentable()
        .values("city", "state")
        .annotate(count=Count("id"))
        .order_by("-count", "city")
    )
    return Response(list(rows))


@api_view(["GET"])
@permission_classes([AllowAny])
def inventory_map_pins(request):
    """
    Pins inside a bounding box.

    THE BOX IS REQUIRED. Without it this is "return every home with
    coordinates", which is a full catalogue export dressed as a map call — and
    the map never needs it, because it always knows its own viewport.
    """
    bounds = {name: request.query_params.get(name) for name in ("north", "south", "east", "west")}
    if not all(bounds.values()):
        return Response(
            {"detail": "north, south, east and west are all required."},
            status=http_status.HTTP_400_BAD_REQUEST,
        )

    try:
        north, south = float(bounds["north"]), float(bounds["south"])
        east, west = float(bounds["east"]), float(bounds["west"])
    except (TypeError, ValueError):
        return Response(
            {"detail": "Bounds must be numbers."},
            status=http_status.HTTP_400_BAD_REQUEST,
        )

    queryset = (
        Property.objects.public()
        .with_total_monthly()
        .filter(
            latitude__isnull=False, longitude__isnull=False,
            latitude__lte=north, latitude__gte=south,
            longitude__lte=east, longitude__gte=west,
        )
    )
    return Response(MapPinSerializer(queryset, many=True).data)


@api_view(["GET"])
@permission_classes([AllowAny])
def inventory_detail(request, slug):
    home = (
        Property.objects.public()
        .with_total_monthly()
        .prefetch_related("images", "fees", "amenities")
        .filter(slug=slug)
        .first()
    )
    if home is None:
        return Response({"detail": "No such home."}, status=http_status.HTTP_404_NOT_FOUND)
    return Response(PublicPropertyDetailSerializer(home, context={'request': request}).data)

@api_view(["POST"])
@permission_classes([IsAuthenticated])
def toggle_favorite(request):
    """
    Save or unsave, in one call.

    A TOGGLE RATHER THAN CREATE + DELETE, because the UI is one heart with one
    meaning. Two endpoints invite a double-tap racing itself into a state where
    the icon and the database disagree, and the caller would have to know which
    one to send — which means reading the current state first, so the round trip
    the optimistic UI exists to avoid comes straight back.

    Idempotent by construction: `get_or_create` against the (user, property)
    unique constraint, so a retried request cannot produce a duplicate row.
    """
    property_id = request.data.get("property")
    if not property_id:
        return Response(
            {"property": "Which home?"}, status=http_status.HTTP_400_BAD_REQUEST,
        )

    # Only publicly visible inventory can be saved: a slug someone guessed for
    # an unpublished draft must not become a way to confirm it exists.
    home = Property.objects.public().filter(id=property_id).first()
    if home is None:
        return Response({"detail": "No such home."}, status=http_status.HTTP_404_NOT_FOUND)

    existing = FavoriteProperty.objects.filter(user=request.user, property=home).first()
    if existing:
        existing.delete()
        return Response({"saved": False})

    FavoriteProperty.objects.get_or_create(user=request.user, property=home)
    return Response({"saved": True}, status=http_status.HTTP_201_CREATED)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def merge_favorites(request):
    """
    Fold a signed-out shortlist into the account on sign-in.

    Someone saves four homes at eleven at night, then registers. Without this
    the list they built is the thing that disappears at the exact moment they
    committed, which is the worst possible time to lose it.

    Additive only — it never deletes. A home already saved on the account stays
    saved, and ids that no longer resolve are skipped rather than failing the
    whole merge.
    """
    ids = request.data.get("properties") or []
    if not isinstance(ids, list):
        return Response(
            {"properties": "Expected a list of ids."}, status=http_status.HTTP_400_BAD_REQUEST,
        )

    homes = Property.objects.public().filter(id__in=[str(i) for i in ids[:50]])
    added = 0
    for home in homes:
        _, created = FavoriteProperty.objects.get_or_create(user=request.user, property=home)
        added += int(created)

    return Response({"added": added, "total": FavoriteProperty.objects.filter(user=request.user).count()})
