"""
Spool processor.

IDEMPOTENT, AND IT GIVES UP. Rows are claimed, attempts counted, and a payload
that fails repeatedly is parked rather than retried forever. A poison message
that blocks the queue means analytics silently stop, which is the failure people
notice six weeks late.

Nothing here is on a request path — it runs from a management command, so it can
take the several writes per event that intake deliberately refuses.
"""

from django.db import transaction
from django.utils import timezone

from .models import PageVisit, RawTelemetryEvent, TelemetryEvent, Visitor, VisitorSession


def _dimension(value):
    """
    A pixel measurement, or nothing.

    Clamped to what a `PositiveSmallIntegerField` holds. These arrive from the
    browser, so a hostile or broken client can send anything at all, and an
    out-of-range integer raises at write time and kills the whole batch.
    """
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if 0 < number <= 32767 else None


def _apply(payload: dict, now) -> None:
    fingerprint = (payload.get("fingerprint") or "").strip()
    session_id = (payload.get("sessionId") or "").strip()
    if not fingerprint or not session_id:
        raise ValueError("Telemetry event without a fingerprint or session id")

    visitor, created = Visitor.objects.get_or_create(
        fingerprint_id=fingerprint[:128],
        defaults={
            "primary_device": (payload.get("deviceType") or "")[:40],
            "primary_city": (payload.get("city") or "")[:100],
        },
    )

    session, session_created = VisitorSession.objects.get_or_create(
        session_id=session_id[:128],
        defaults={
            "visitor": visitor,
            "ip_address": (payload.get("ip") or "")[:64],
            "city": (payload.get("city") or "")[:100],
            "browser": (payload.get("browser") or "")[:60],
            "os": (payload.get("os") or "")[:60],
            "device_type": (payload.get("deviceType") or "")[:40],
            "landing_page": (payload.get("path") or "")[:300],
            "referrer": (payload.get("referrer") or "")[:500],
            "utm_source": (payload.get("utmSource") or "")[:100],
            # Everything the client and the edge were already sending. It had
            # nowhere to go until these columns existed.
            "country": (payload.get("country") or "")[:100],
            "region": (payload.get("region") or "")[:100],
            "timezone": (payload.get("timezone") or "")[:64],
            "language": (payload.get("language") or "")[:20],
            "user_agent": (payload.get("userAgent") or "")[:300],
            "screen_width": _dimension(payload.get("screenWidth")),
            "screen_height": _dimension(payload.get("screenHeight")),
            "viewport_width": _dimension(payload.get("viewportWidth")),
            "viewport_height": _dimension(payload.get("viewportHeight")),
        },
    )
    if session_created:
        Visitor.objects.filter(pk=visitor.pk).update(
            total_sessions_count=visitor.total_sessions_count + 1,
        )

    event_type = (payload.get("event") or "event")[:60]
    path = (payload.get("path") or "")[:300]

    page_visit = None
    if path:
        if event_type == "page_view":
            page_visit = PageVisit.objects.create(session=session, path=path)
        else:
            # Attach to the open visit for this path rather than creating a
            # second row — an exit is the same visit as its entry.
            page_visit = (
                PageVisit.objects.filter(session=session, path=path, exit_time__isnull=True)
                .order_by("-entry_time")
                .first()
            )

    if event_type == "page_exit" and page_visit is not None:
        dwell = int(payload.get("dwellSeconds") or 0)
        page_visit.exit_time = now
        page_visit.max_scroll_depth = min(100, int(payload.get("scrollDepth") or 0))
        page_visit.save(update_fields=["exit_time", "max_scroll_depth"])

        VisitorSession.objects.filter(pk=session.pk).update(
            end_time=now, total_dwell_seconds=session.total_dwell_seconds + dwell,
        )
        Visitor.objects.filter(pk=visitor.pk).update(
            total_dwell_seconds=visitor.total_dwell_seconds + dwell,
        )

    TelemetryEvent.objects.create(
        session=session, page_visit=page_visit,
        event_type=event_type, event_data=payload,
    )


def process_spool(batch_size: int = 200) -> dict:
    now = timezone.now()
    rows = RawTelemetryEvent.objects.filter(
        processed=False, attempts__lt=RawTelemetryEvent.MAX_ATTEMPTS,
    ).order_by("received_at")[:batch_size]

    processed = failed = 0
    for row in rows:
        try:
            with transaction.atomic():
                _apply(row.payload or {}, now)
                RawTelemetryEvent.objects.filter(pk=row.pk).update(processed=True)
            processed += 1
        except Exception as exc:  # noqa: BLE001 — a bad payload must not stop the queue
            RawTelemetryEvent.objects.filter(pk=row.pk).update(
                attempts=row.attempts + 1, last_error=str(exc)[:500],
            )
            failed += 1

    parked = RawTelemetryEvent.objects.filter(
        processed=False, attempts__gte=RawTelemetryEvent.MAX_ATTEMPTS,
    ).count()
    return {"processed": processed, "failed": failed, "parked": parked}
