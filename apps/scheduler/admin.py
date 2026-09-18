from pathlib import Path
from django.contrib import admin
from django.utils.html import format_html
from django.urls import reverse
from unfold.admin import ModelAdmin as UnfoldModelAdmin

from .models import TourRequest, Viewing

@admin.register(TourRequest)
class TourRequestAdmin(UnfoldModelAdmin):
    list_display = ["full_name", "status", "property", "preferred_date", "created_at"]
    list_filter = ["status", "tour_type"]
    readonly_fields = ["id_front_link", "id_back_link", "id_purged_at", "public_id"]
    search_fields = ["full_name", "email", "phone"]
    
    def id_front_link(self, obj):
        if obj.id_front_url:
            filename = Path(obj.id_front_url).name
            view_url = reverse("secure-tour-id-view", kwargs={"filename": filename})
            download_url = f"{view_url}?download=1"
            return format_html(
                '<a href="{}" target="_blank" download style="display:inline-block;margin-bottom:10px;text-decoration:underline;color:#0b6b47;">'
                '<strong>Download Front ID ⬇️</strong></a><br/>'
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
                '<strong>Download Back ID ⬇️</strong></a><br/>'
                '<a href="{}" target="_blank">'
                '<img src="{}" style="max-width:300px;border-radius:8px;border:1px solid #ddd" /></a>',
                download_url, view_url, view_url
            )
        return "Not uploaded"
    id_back_link.short_description = "Back ID"

@admin.register(Viewing)
class ViewingAdmin(UnfoldModelAdmin):
    pass
