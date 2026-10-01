from pathlib import Path
from django.contrib import admin
from django.utils.html import format_html
from django.urls import reverse
from unfold.admin import ModelAdmin as UnfoldModelAdmin
from unfold.decorators import display

from .models import TourRequest, Viewing


class IdOnFileFilter(admin.SimpleListFilter):
    """Which requests still need a document before anyone can approve them."""

    title = "ID on file"
    parameter_name = "id_on_file"

    def lookups(self, request, model_admin):
        return [
            ("both", "Front and back"),
            ("partial", "One side only"),
            ("none", "None"),
        ]

    def queryset(self, request, queryset):
        if self.value() == "both":
            return queryset.exclude(id_front_url="").exclude(id_back_url="")
        if self.value() == "partial":
            return (
                queryset.filter(id_front_url="").exclude(id_back_url="")
                | queryset.exclude(id_front_url="").filter(id_back_url="")
            )
        if self.value() == "none":
            return queryset.filter(id_front_url="", id_back_url="")
        return queryset


@admin.register(TourRequest)
class TourRequestAdmin(UnfoldModelAdmin):
    list_display = [
        "full_name", "property", "preferred_date", "time_window", "tour_type",
        "id_status", "status", "created_at",
    ]
    list_filter = ["status", "tour_type", "time_is_custom", IdOnFileFilter, "preferred_date"]
    date_hierarchy = "preferred_date"
    readonly_fields = [
        "time_window", "id_front_link", "id_back_link", "id_purged_at", "public_id", "created_at",
    ]
    search_fields = ["full_name", "email", "phone"]
    fieldsets = [
        ("Who", {"fields": ["full_name", "email", "phone", "lead"]}),
        ("Tour", {
            "fields": [
                "property", "tour_type", "preferred_date", "time_window",
                ("time_start", "time_end", "time_is_custom"), "preferred_time", "notes",
            ],
        }),
        ("ID", {"fields": ["id_front_link", "id_back_link", "id_purged_at"]}),
        ("Review", {
            "fields": [
                "status", "reviewed_by", "reviewed_at", "rejection_reason", "viewing",
                "public_id", "created_at",
            ],
        }),
    ]

    @display(description="Time window", ordering="time_start")
    def time_window(self, obj):
        label = obj.time_window_label() or "-"
        if obj.time_is_custom:
            return format_html("{} <em style=\"opacity:.7\">(custom)</em>", label)
        return label

    @display(
        description="ID",
        label={"Front + back": "success", "Front only": "warning", "Back only": "warning",
               "Missing": "danger", "Purged": "info"},
    )
    def id_status(self, obj):
        if obj.id_purged_at:
            return "Purged"
        front, back = bool(obj.id_front_url), bool(obj.id_back_url)
        if front and back:
            return "Front + back"
        if front:
            return "Front only"
        if back:
            return "Back only"
        return "Missing"

    def id_front_link(self, obj):
        if obj.id_front_url:
            filename = Path(obj.id_front_url).name
            view_url = reverse("secure-tour-id-view", kwargs={"filename": filename})
            download_url = f"{view_url}?download=1"
            return format_html(
                '<a href="{}" target="_blank" download style="display:inline-block;margin-bottom:10px;text-decoration:underline;color:#0b6b47;">'
                '<strong>Download Front ID</strong></a><br/>'
                '<a href="{}" target="_blank">'
                '<img src="{}" style="max-width:300px;border-radius:8px;border:1px solid #ddd" /></a>',
                download_url, view_url, view_url
            )
        return "Not uploaded"
    id_front_link.short_description = "Front ID"

    def id_back_link(self, obj):
        if obj.id_back_url:
            filename = Path(obj.id_back_url).name
            view_url = reverse("secure-tour-id-view", kwargs={"filename": filename})
            download_url = f"{view_url}?download=1"
            return format_html(
                '<a href="{}" target="_blank" download style="display:inline-block;margin-bottom:10px;text-decoration:underline;color:#0b6b47;">'
                '<strong>Download Back ID</strong></a><br/>'
                '<a href="{}" target="_blank">'
                '<img src="{}" style="max-width:300px;border-radius:8px;border:1px solid #ddd" /></a>',
                download_url, view_url, view_url
            )
        return "Not uploaded"
    id_back_link.short_description = "Back ID"

@admin.register(Viewing)
class ViewingAdmin(UnfoldModelAdmin):
    pass
