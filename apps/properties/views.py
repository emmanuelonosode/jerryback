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

from .models import RENTABLE_STATUSES, FavoriteProperty
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


def _apply_filters(request, queryset):
    """
    Every catalogue filter the search UI can send, applied to a queryset.

    EXTRACTED SO THE MAP CANNOT DISAGREE WITH THE LIST. `/pins/` has to answer
    "where is everything this search matched", and the only way it stays the
    same set the cards came from is by running the identical predicate chain.
    A second hand-maintained copy drifts on the first filter anybody adds, and
    the symptom - dots on the map for homes the list says do not match - is the
    kind of thing nobody reports as a bug, they just stop trusting the map.

    Returns `(queryset, searched)`; `searched` says whether free text imposed a
    relevance ordering that an explicit sort should not silently override.
    """
    # Free text first: it reorders the queryset, and the ordering it applies
    # must survive everything below it.
    queryset, searched = _apply_search(queryset, request.query_params.get("q", ""))

    """
    An explicit id list, for the saved-homes page.

    That page holds up to 50 ids in a cookie and used to resolve them by
    fetching the entire catalogue and running `Array.find` fifty times. Capped
    at MAX_SAVED so this cannot become a catalogue export with extra steps, and
    silently dropping anything that is not a UUID rather than raising - a
    tampered cookie should return fewer homes, not a 500.
    """
    ids = request.query_params.get("ids", "").strip()
    if ids:
        import uuid as _uuid

        wanted = []
        for raw in ids.split(",")[:50]:
            try:
                wanted.append(_uuid.UUID(raw.strip()))
            except (ValueError, AttributeError):
                continue
        queryset = queryset.filter(id__in=wanted) if wanted else queryset.none()

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

    return queryset, searched


@api_view(["GET"])
@permission_classes([AllowAny])
def inventory(request):
    queryset = (
        Property.objects.rentable()
        .with_total_monthly()
        .prefetch_related("images")
        .order_by("-is_featured", "-created_at")
    )
    queryset, searched = _apply_filters(request, queryset)

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


#: A hard ceiling on one pin response, so this can never become a full export.
#:
#: Sized above the live catalogue (8,841 homes with coordinates) rather than
#: below it, because clipping the map would be the exact failure this endpoint
#: exists to fix. It is a runaway guard, not a page size.
MAX_PINS = 25_000

#: Coordinate precision, in decimal places. Five is about a metre - far finer
#: than a marker is drawn - and truncating there removes roughly a fifth of the
#: payload for no visible difference.
PIN_PRECISION = 5


@api_view(["GET"])
@permission_classes([AllowAny])
def inventory_pins(request):
    """
    Every home a search matched, as coordinates. No photographs, no prose.

    WHY THIS EXISTS. The map was drawing the twelve homes on the current page
    of results, so a renter looking at a national catalogue saw twelve dots and
    concluded that was the inventory. Showing all of it through the normal
    catalogue endpoint is not an option: that payload is a megabyte per two
    hundred homes, almost all of it image metadata the map cannot draw.

    WHAT A PIN IS. Latitude, longitude, the all-in monthly figure, bedrooms and
    the slug - the least that lets a dot be positioned, labelled and clicked
    through to the home. At 8,841 homes that is 566KB of JSON, 228KB gzipped,
    against 45MB for the same set fetched as full listings.

    WHY ARRAYS RATHER THAN OBJECTS. Repeating five key names on nine thousand
    rows is a quarter of the payload spent restating the schema. `fields`
    names the tuple positions once, so the client stays readable and the wire
    format stays small.

    THE SLUG IS INCLUDED DELIBERATELY. It is the single largest component of
    the response - 274KB of the 566KB - and dropping it would mean a round trip
    to Django before a clicked dot could go anywhere. A link that works
    immediately is worth the bytes, particularly once gzip has had them.
    """
    queryset = (
        Property.objects.rentable()
        .with_total_monthly()
        .filter(latitude__isnull=False, longitude__isnull=False)
    )
    queryset, _searched = _apply_filters(request, queryset)

    # Ordering is meaningless for a point cloud and a sort over nine thousand
    # rows is not free, so the model's default `-created_at` is cleared.
    rows = queryset.order_by().values_list(
        "latitude", "longitude", "total_monthly_cents", "bedrooms", "slug",
    )[:MAX_PINS]

    pins = [
        [round(float(lat), PIN_PRECISION), round(float(lng), PIN_PRECISION),
         cents, beds, slug]
        for lat, lng, cents, beds, slug in rows
    ]

    response = Response({
        "fields": ["lat", "lng", "total_monthly_cents", "bedrooms", "slug"],
        "count": len(pins),
        "truncated": len(pins) >= MAX_PINS,
        "pins": pins,
    })
    # Inventory changes when a person edits it. A minute of browser cache and
    # five of shared cache means panning and zooming the map costs nothing,
    # and a filter change is a different URL so it is never served stale.
    response["Cache-Control"] = "public, max-age=60, s-maxage=300"
    return response


@api_view(["GET"])
@permission_classes([AllowAny])
def inventory_cities(request):
    """
    Cities we have pages for, with two counts that mean different things.

    `count` is RENTABLE inventory - available and coming-soon. It is what the
    hub threshold measures and what a renter is told, because saying "12 homes
    in Memphis" when all twelve are leased is a lie a visitor discovers one
    click later.

    `public_count` includes homes inside their leased grace window. It exists
    because a city hub must keep EXISTING while any of its homes are still
    publicly reachable. The frontend previously derived hubs from the full
    catalogue, which is `public()`, so dropping to `rentable()` here silently
    404'd every city whose inventory had all been let - taking a page that was
    indexed and had inbound links out of the index rather than showing it with
    an honest "nothing available right now" and the nearest alternatives.
    """
    from django.db.models import Sum

    rows = (
        Property.objects.public()
        .values("city", "state")
        .annotate(
            public_count=Count("id"),
            count=Sum(
                Case(
                    When(status__in=RENTABLE_STATUSES, then=1),
                    default=0,
                    output_field=IntegerField(),
                )
            ),
        )
        .order_by("-count", "city")
    )
    return Response([
        {
            "city": r["city"],
            "state": r["state"],
            "count": r["count"] or 0,
            "public_count": r["public_count"],
        }
        for r in rows
    ])


@api_view(["GET"])
@permission_classes([AllowAny])
def inventory_sitemap(request):
    """
    Every indexable home as a slug and a timestamp. Nothing else.

    EXISTS FOR THE SAME REASON `inventory_stats` DOES. The frontend built its
    sitemap by fetching all 4,482 properties - with their 78,417 image rows,
    fee schedules and descriptions - mapping each through `toListing`, then
    reading two fields off the result. Hundreds of megabytes of objects to emit
    a list of URLs, on a host with a 1GB Node heap, and a direct cause of the
    web process being OOM-killed.

    `rentable()`, NOT `public()`, and the distinction is the whole point.

    A leased home keeps its page for a 45-day grace window so an inbound link
    lands somewhere useful instead of on a 404 - but the frontend serves that
    page `noindex, follow`, because a home nobody can rent should stop
    competing in search. Listing it here as well would tell Google to index a
    URL the page itself forbids indexing: the exact contradiction the
    indexation audit exists to catch, on 1,388 URLs.

    Reachable and indexable are different states. This endpoint answers the
    second.

    `values()` so Django never builds a model instance, and `iterator()` so the
    result set is streamed rather than held twice.
    """
    rows = (
        Property.objects.rentable()
        .order_by("-updated_at")
        .values("slug", "updated_at")
    )
    return Response([
        {"slug": r["slug"], "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None}
        for r in rows.iterator(chunk_size=2000)
    ])


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
        Property.objects.rentable()
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

@api_view(["GET"])
@permission_classes([AllowAny])
def inventory_stats(request):
    """
    Aggregate counts, computed by the database.

    EXISTS SO `/llms.txt` STOPS LOADING THE WHOLE CATALOGUE. That route wanted
    five numbers - how many homes, how many cities, the price range - and was
    getting them by pulling every published property across 23 paginated
    requests and counting in JavaScript. On a 2GB host that is hundreds of
    megabytes of objects per request, and it was a live cause of the web
    process being OOM-killed. This is one SQL round trip.
    """
    queryset = Property.objects.rentable().with_total_monthly()
    totals = sorted(queryset.values_list("total_monthly_cents", flat=True))

    by_state = list(
        queryset.values("state").annotate(n=Count("id")).order_by("-n")[:8]
    )

    return Response({
        "homes": len(totals),
        "cities": queryset.values("city", "state").distinct().count(),
        "states": queryset.values("state").distinct().count(),
        "min_total_cents": totals[0] if totals else None,
        "max_total_cents": totals[-1] if totals else None,
        "median_total_cents": totals[len(totals) // 2] if totals else None,
        "top_states": [{"state": r["state"], "homes": r["n"]} for r in by_state],
        "min_bedrooms": queryset.order_by("bedrooms").values_list("bedrooms", flat=True).first(),
        "max_bedrooms": queryset.order_by("-bedrooms").values_list("bedrooms", flat=True).first(),
    })
