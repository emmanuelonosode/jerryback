"""
Telemetry intake.

ONE INSERT AND A 204. This sits behind a page a visitor is using, so it writes
the raw payload to the spool and returns. Resolving the visitor, upserting the
session and updating dwell all happen in the processor that drains it — doing
them inline would put several writes in front of a response the browser is
waiting on, and an analytics failure would become a user-visible one.

PUBLIC, BUT NOT A WRITE PRIMITIVE. The endpoint is unauthenticated because the
visitors it measures are not signed in. It is throttled, size-capped and
batch-capped, and it writes to a spool that only the processor reads — so the
worst a flood achieves is rows in a table nobody serves from.

THE CLIENT CANNOT SET ITS OWN IP OR GEOGRAPHY. Both are resolved from the
request here and overwrite whatever the payload claimed, and the IP is
truncated to its network before it is stored.
"""

import json

from rest_framework import status as http
from rest_framework.decorators import api_view, permission_classes, throttle_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle

from .models import RawTelemetryEvent, truncate_ip

MAX_BODY_BYTES = 16_000
MAX_BATCH = 40


class TelemetryThrottle(ScopedRateThrottle):
    scope = "telemetry"


def client_ip(request) -> str | None:
    """
    The original client, from the proxy chain.

    `X-Forwarded-For` is appended to by each hop, so the client is the LEFTMOST
    entry — taking the last gives our own load balancer on every request.
    """
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if forwarded:
        first = forwarded.split(",")[0].strip()
        if first:
            return first
    return request.META.get("HTTP_X_REAL_IP") or request.META.get("REMOTE_ADDR")


def _geo(request) -> dict:
    """
    Geography from edge headers, never from a lookup service.

    CDNs resolve this at the edge and pass it along — free, instant, already
    computed. The alternative is posting every visitor's full IP to a
    third-party geolocation API, which sends the one field this system
    deliberately truncates, whole, to somebody else. An absent header means an
    empty city, which is an honest gap; a guessed one poisons every report.
    """
    def read(*names):
        for name in names:
            value = request.META.get(name, "").strip()
            if value:
                return value[:100]
        return ""

    return {
        "country": read("HTTP_X_VERCEL_IP_COUNTRY", "HTTP_CF_IPCOUNTRY", "HTTP_X_GEO_COUNTRY"),
        "region": read("HTTP_X_VERCEL_IP_COUNTRY_REGION", "HTTP_CF_REGION_CODE", "HTTP_X_GEO_REGION"),
        "city": read("HTTP_X_VERCEL_IP_CITY", "HTTP_CF_IPCITY", "HTTP_X_GEO_CITY"),
    }


@api_view(["POST"])
@permission_classes([AllowAny])
@throttle_classes([TelemetryThrottle])
def collect(request):
    raw = request.body or b""
    if len(raw) > MAX_BODY_BYTES:
        return Response(status=http.HTTP_413_REQUEST_ENTITY_TOO_LARGE)

    try:
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return Response(status=http.HTTP_400_BAD_REQUEST)

    events = payload[:MAX_BATCH] if isinstance(payload, list) else [payload]
    network = truncate_ip(client_ip(request))
    geo = _geo(request)

    rows = [
        RawTelemetryEvent(payload={**event, "ip": network, **geo})
        for event in events
        if isinstance(event, dict)
    ]
    if rows:
        RawTelemetryEvent.objects.bulk_create(rows)

    # 204: sendBeacon ignores the response, and there is nothing a caller could
    # usefully do with a body.
    return Response(status=http.HTTP_204_NO_CONTENT)
