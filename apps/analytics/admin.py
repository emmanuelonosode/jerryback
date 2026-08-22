from django.contrib import admin
from unfold.admin import ModelAdmin as UnfoldModelAdmin

from .models import PageVisit, RawTelemetryEvent, TelemetryEvent, Visitor, VisitorSession


@admin.register(Visitor)
class VisitorAdmin(UnfoldModelAdmin):
    list_display = ("fingerprint_id", "primary_device", "primary_city",
                    "total_sessions_count", "total_dwell_seconds", "last_seen")
    list_filter = ("primary_device", "is_lead")
    search_fields = ("fingerprint_id", "primary_city")
    readonly_fields = ("first_seen", "last_seen")


@admin.register(VisitorSession)
class VisitorSessionAdmin(UnfoldModelAdmin):
    list_display = ("session_id", "city", "device_type", "browser", "os",
                    "landing_page", "total_dwell_seconds", "start_time")
    list_filter = ("device_type", "browser", "os")
    search_fields = ("session_id", "city", "landing_page", "referrer")
    readonly_fields = ("start_time",)


@admin.register(PageVisit)
class PageVisitAdmin(UnfoldModelAdmin):
    list_display = ("path", "session", "entry_time", "exit_time", "max_scroll_depth")
    list_filter = ("max_scroll_depth",)
    search_fields = ("path",)
    readonly_fields = ("entry_time",)


@admin.register(TelemetryEvent)
class TelemetryEventAdmin(UnfoldModelAdmin):
    list_display = ("event_type", "session", "page_visit", "created_at")
    list_filter = ("event_type",)
    readonly_fields = ("created_at",)


@admin.register(RawTelemetryEvent)
class RawTelemetryEventAdmin(UnfoldModelAdmin):
    """
    The spool. Visible so a stuck queue is discoverable rather than silent —
    `last_error` on a parked row is usually the whole diagnosis.
    """

    list_display = ("id", "received_at", "processed", "attempts", "last_error")
    list_filter = ("processed",)
    readonly_fields = ("payload", "received_at")
