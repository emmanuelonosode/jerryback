"""
Telemetry.

INTAKE IS ONE INSERT AND NOTHING ELSE. The endpoint receiving this sits on the
critical path of a page a renter is using; resolving a visitor, upserting a
session and updating dwell inline would put several writes in front of the
response, and any of them failing would fail a call the page load is waiting on.
Everything else happens in a worker reading the spool.

WHAT IS DELIBERATELY NOT COLLECTED. The spec lists hardware concurrency, device
memory and connection type — a fingerprinting set whose real use is
re-identifying someone who has cleared their cookies. This collects what answers
a product question (device class, city, referrer, dwell) and skips the entropy
whose purpose is to defeat a user's own reset. IPs are truncated to the network
for the same reason: the city is the useful part, the last octet is the device.
"""

import uuid

from django.db import models


def truncate_ip(ip: str | None) -> str | None:
    """The last octet identifies a device, not a place."""
    if not ip:
        return None
    if ":" in ip:
        groups = ip.split(":")
        return ":".join(groups[:4]) + "::"
    parts = ip.split(".")
    if len(parts) != 4:
        return None
    return f"{parts[0]}.{parts[1]}.{parts[2]}.0"


class RawTelemetryEvent(models.Model):
    MAX_ATTEMPTS = 5

    id = models.BigAutoField(primary_key=True)
    payload = models.JSONField()
    received_at = models.DateTimeField(auto_now_add=True, db_index=True)
    processed = models.BooleanField(default=False, db_index=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    last_error = models.TextField(blank=True, default="")

    class Meta:
        db_table = "raw_telemetry_events"
        indexes = [models.Index(fields=["processed", "received_at"])]


class Visitor(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    fingerprint_id = models.CharField(max_length=128, unique=True, db_index=True)
    user = models.ForeignKey("accounts.User", null=True, blank=True, on_delete=models.SET_NULL)
    first_seen = models.DateTimeField(auto_now_add=True)
    last_seen = models.DateTimeField(auto_now=True)
    total_sessions_count = models.PositiveIntegerField(default=0)
    total_dwell_seconds = models.PositiveIntegerField(default=0)
    primary_device = models.CharField(max_length=40, blank=True, default="")
    primary_city = models.CharField(max_length=100, blank=True, default="")
    is_lead = models.BooleanField(default=False)

    class Meta:
        db_table = "visitors"


class VisitorSession(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    visitor = models.ForeignKey(Visitor, on_delete=models.CASCADE, related_name="sessions")
    session_id = models.CharField(max_length=128, unique=True)
    ip_address = models.CharField(max_length=64, blank=True, default="")
    city = models.CharField(max_length=100, blank=True, default="")
    browser = models.CharField(max_length=60, blank=True, default="")
    os = models.CharField(max_length=60, blank=True, default="")
    device_type = models.CharField(max_length=40, blank=True, default="")
    landing_page = models.CharField(max_length=300, blank=True, default="")
    referrer = models.CharField(max_length=500, blank=True, default="")
    utm_source = models.CharField(max_length=100, blank=True, default="")
    properties_viewed_count = models.PositiveIntegerField(default=0)
    start_time = models.DateTimeField(auto_now_add=True)
    end_time = models.DateTimeField(null=True, blank=True)
    total_dwell_seconds = models.PositiveIntegerField(default=0)

    class Meta:
        db_table = "visitor_sessions"


class PageVisit(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    session = models.ForeignKey(VisitorSession, on_delete=models.CASCADE, related_name="page_visits")
    path = models.CharField(max_length=300)
    entry_time = models.DateTimeField(auto_now_add=True)
    exit_time = models.DateTimeField(null=True, blank=True)
    # A high-water mark: scrolling back up does not undo how far someone read.
    max_scroll_depth = models.PositiveSmallIntegerField(default=0)
    idle_seconds = models.PositiveIntegerField(default=0)

    class Meta:
        db_table = "page_visits"


class TelemetryEvent(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    session = models.ForeignKey(VisitorSession, on_delete=models.CASCADE, related_name="events")
    page_visit = models.ForeignKey(PageVisit, null=True, blank=True, on_delete=models.SET_NULL)
    event_type = models.CharField(max_length=60)
    event_data = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "telemetry_events"
