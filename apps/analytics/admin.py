from django.contrib import admin
from django.urls import reverse
from django.utils.html import format_html
from django.utils.timesince import timesince
from unfold.admin import ModelAdmin as UnfoldModelAdmin, TabularInline as UnfoldTabularInline

from .models import PageVisit, RawTelemetryEvent, TelemetryEvent, Visitor, VisitorSession


def _dwell(seconds: int) -> str:
    """Seconds are not readable at a glance; 4m 12s is."""
    seconds = int(seconds or 0)
    if seconds < 60:
        return f"{seconds}s"
    return f"{seconds // 60}m {seconds % 60}s"


class PageVisitInline(UnfoldTabularInline):
    """
    The pages, in order, inside the session that produced them.

    A visit is only meaningful in sequence — which page they landed on, where
    they went next, how far down they got — and that was previously three
    separate screens with a session id copied between them.
    """

    model = PageVisit
    extra = 0
    can_delete = False
    fields = ("path", "entry_time", "seconds_on_page", "max_scroll_depth", "idle_seconds")
    readonly_fields = fields
    ordering = ("entry_time",)

    @admin.display(description="Time on page")
    def seconds_on_page(self, obj):
        if not obj.exit_time or not obj.entry_time:
            return "—"
        return _dwell((obj.exit_time - obj.entry_time).total_seconds())

    def has_add_permission(self, request, obj=None):
        return False


class TelemetryEventInline(UnfoldTabularInline):
    model = TelemetryEvent
    extra = 0
    can_delete = False
    fields = ("event_type", "page_visit", "event_data", "created_at")
    readonly_fields = fields
    ordering = ("created_at",)

    def has_add_permission(self, request, obj=None):
        return False


class SessionInline(UnfoldTabularInline):
    model = VisitorSession
    extra = 0
    can_delete = False
    fields = ("open_session", "start_time", "device_type", "browser", "where", "referrer", "dwell")
    readonly_fields = fields
    ordering = ("-start_time",)

    @admin.display(description="Session")
    def open_session(self, obj):
        url = reverse("admin:analytics_visitorsession_change", args=[obj.pk])
        return format_html('<a href="{}">Open</a>', url)

    @admin.display(description="Where")
    def where(self, obj):
        return ", ".join(p for p in (obj.city, obj.region, obj.country) if p) or "—"

    @admin.display(description="Dwell")
    def dwell(self, obj):
        return _dwell(obj.total_dwell_seconds)

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(Visitor)
class VisitorAdmin(UnfoldModelAdmin):
    """One person across every visit they have made."""

    list_display = (
        "fingerprint_short", "primary_device", "primary_city",
        "total_sessions_count", "dwell", "is_lead", "last_seen_ago",
    )
    list_filter = ("primary_device", "is_lead", "first_seen", "last_seen")
    search_fields = ("fingerprint_id", "primary_city", "sessions__ip_address",
                     "sessions__city", "sessions__country", "sessions__referrer")
    readonly_fields = ("fingerprint_id", "user", "first_seen", "last_seen",
                       "total_sessions_count", "total_dwell_seconds",
                       "primary_device", "primary_city", "is_lead")
    inlines = [SessionInline]
    ordering = ("-last_seen",)
    date_hierarchy = "last_seen"

    @admin.display(description="Visitor", ordering="fingerprint_id")
    def fingerprint_short(self, obj):
        # The full hash is 128 characters and unreadable in a list. It is still
        # searchable in full, and shown in full on the record.
        return obj.fingerprint_id[:12]

    @admin.display(description="Total time", ordering="total_dwell_seconds")
    def dwell(self, obj):
        return _dwell(obj.total_dwell_seconds)

    @admin.display(description="Last seen", ordering="last_seen")
    def last_seen_ago(self, obj):
        return f"{timesince(obj.last_seen)} ago"

    def has_add_permission(self, request):
        return False


@admin.register(VisitorSession)
class VisitorSessionAdmin(UnfoldModelAdmin):
    """
    One visit, with everything known about it.

    The list is what a session is: when, from where, on what, arriving from
    where, and how long they stayed. Everything else is on the record.
    """

    list_display = (
        "start_time", "where", "device", "browser", "os",
        "came_from", "landing_page", "dwell", "pages",
    )
    list_filter = ("device_type", "browser", "os", "country", "timezone", "start_time")
    search_fields = ("session_id", "ip_address", "city", "region", "country",
                     "referrer", "landing_page", "utm_source", "user_agent")
    ordering = ("-start_time",)
    date_hierarchy = "start_time"
    inlines = [PageVisitInline, TelemetryEventInline]

    readonly_fields = tuple(f.name for f in VisitorSession._meta.fields) + ("screen", "viewport")

    fieldsets = (
        ("This visit", {
            "fields": ("visitor", "session_id", "start_time", "end_time",
                       "total_dwell_seconds", "properties_viewed_count"),
        }),
        ("Where they came from", {
            "fields": ("referrer", "utm_source", "landing_page"),
        }),
        ("Where they are", {
            "description": (
                "The address is truncated to its /24 network before it is stored — "
                "the last octet identifies a device rather than a place. Geography "
                "comes from the CDN's edge headers, never from a lookup service, so "
                "a blank is an honest gap rather than a guess."
            ),
            "fields": ("ip_address", "city", "region", "country", "timezone", "language"),
        }),
        ("What they used", {
            "fields": ("device_type", "browser", "os", "screen", "viewport", "user_agent"),
        }),
    )

    @admin.display(description="Where")
    def where(self, obj):
        place = ", ".join(p for p in (obj.city, obj.region, obj.country) if p)
        if obj.timezone:
            place = f"{place} · {obj.timezone}" if place else obj.timezone
        return place or "—"

    @admin.display(description="Device", ordering="device_type")
    def device(self, obj):
        return obj.device_type or "—"

    @admin.display(description="Came from")
    def came_from(self, obj):
        """
        The referring site, not the whole URL.
        
        A full referrer is often 200 characters of query string; the host is
        the part that answers "where did this person come from".
        """
        if obj.utm_source:
            return format_html("<b>{}</b>", obj.utm_source)
        if not obj.referrer:
            return "Direct"
        host = obj.referrer.split("//")[-1].split("/")[0]
        return host or "Direct"

    @admin.display(description="Dwell", ordering="total_dwell_seconds")
    def dwell(self, obj):
        return _dwell(obj.total_dwell_seconds)

    @admin.display(description="Pages")
    def pages(self, obj):
        return obj.page_visits.count()

    @admin.display(description="Screen")
    def screen(self, obj):
        if not obj.screen_width:
            return "—"
        return f"{obj.screen_width} × {obj.screen_height}"

    @admin.display(description="Window")
    def viewport(self, obj):
        if not obj.viewport_width:
            return "—"
        return f"{obj.viewport_width} × {obj.viewport_height}"

    def has_add_permission(self, request):
        return False


@admin.register(PageVisit)
class PageVisitAdmin(UnfoldModelAdmin):
    list_display = ("path", "entry_time", "max_scroll_depth", "idle_seconds", "session")
    list_filter = ("max_scroll_depth", "entry_time")
    search_fields = ("path", "session__session_id", "session__city")
    readonly_fields = tuple(f.name for f in PageVisit._meta.fields)
    date_hierarchy = "entry_time"
    ordering = ("-entry_time",)

    def has_add_permission(self, request):
        return False


@admin.register(TelemetryEvent)
class TelemetryEventAdmin(UnfoldModelAdmin):
    list_display = ("event_type", "created_at", "session", "page_visit")
    list_filter = ("event_type", "created_at")
    search_fields = ("event_type", "session__session_id")
    readonly_fields = tuple(f.name for f in TelemetryEvent._meta.fields)
    date_hierarchy = "created_at"
    ordering = ("-created_at",)

    def has_add_permission(self, request):
        return False


@admin.register(RawTelemetryEvent)
class RawTelemetryEventAdmin(UnfoldModelAdmin):
    """
    The spool. Visible so a stuck queue is discoverable rather than silent —
    `last_error` on a parked row is usually the whole diagnosis.
    """

    list_display = ("id", "received_at", "processed", "attempts", "last_error")
    list_filter = ("processed", "received_at")
    readonly_fields = ("payload", "received_at")
    ordering = ("-received_at",)

    def has_add_permission(self, request):
        return False
