"""
Inventory admin.

Money is shown formatted and edited in cents. That asymmetry is deliberate: a
dollars-and-cents input invites a float somewhere, and the list display is where
a person actually reads the number.
"""

from django.contrib import admin
from unfold.admin import ModelAdmin as UnfoldModelAdmin, TabularInline as UnfoldTabularInline, StackedInline as UnfoldStackedInline
from django.utils.html import format_html

from apps.core.money import format_usd

from .models import (
    AmenityCategory, Property, PropertyAmenity, PropertyFee, PropertyImage,
)


class PropertyImageInline(UnfoldTabularInline):
    model = PropertyImage
    extra = 0
    fields = ("url", "caption", "is_primary", "sort_order", "source_url")
    readonly_fields = ("source_url",)


class PropertyFeeInline(UnfoldTabularInline):
    model = PropertyFee
    extra = 0
    fields = ("label", "fee_key", "amount_cents", "cadence", "condition", "applies_when", "reason")


class PropertyAmenityInline(UnfoldTabularInline):
    model = PropertyAmenity
    extra = 0


@admin.register(Property)
class PropertyAdmin(UnfoldModelAdmin):
    list_display = (
        "address", "city", "state", "status", "total_monthly_display",
        "voucher_accepted", "is_published", "staleness",
    )
    list_filter = ("status", "is_published", "voucher_accepted", "pets_allowed", "state", "type")
    search_fields = ("address", "city", "zip_code", "slug", "title")
    readonly_fields = ("slug", "original_price_cents", "created_at", "updated_at", "total_monthly_display", "schools", "raw_fees", "office_info", "floor_plans")
    inlines = [PropertyImageInline, PropertyFeeInline, PropertyAmenityInline]
    actions = ["mark_verified", "publish", "unpublish"]
    fieldsets = (
        ("Listing", {"fields": ("title", "slug", "description", "type", "status", "is_published")}),
        ("Pricing", {
            "fields": ("price_cents", "original_price_cents", "price_label", "total_monthly_display"),
            "description": "Amounts are in CENTS. The total shown includes every required monthly fee — "
                           "that is the figure the public site advertises, never base rent alone.",
        }),
        ("Specification", {"fields": ("bedrooms", "half_bathrooms", "sqft", "year_built", "garage", "stories")}),
        ("Home detail", {
            "fields": ("parking", "laundry", "hvac", "flooring", "appliances"),
            "description": "Rendered as the specification table on the public listing page.",
        }),
        ("Virtual tour", {
            "fields": ("tour_3d_url", "tour_video_url"),
            "description": "Matterport, Kuula, Momento360, YouTube or Vimeo only. Any other host "
                           "is refused by the public site rather than framed — an arbitrary origin "
                           "inside our frame could serve a convincing fake sign-in.",
        }),
        ("Location", {"fields": ("address", "city", "state", "zip_code", "latitude", "longitude", "neighborhood")}),
        ("Who can rent it", {
            "fields": ("voucher_accepted", "pets_allowed", "pet_policy", "accessibility_features"),
            "description": "Voucher acceptance is promised on every page of the public site and is a "
                           "search filter. It is not in any partner feed, so it is maintained here.",
        }),
        ("Availability", {"fields": ("available_from", "leased_at", "last_verified_at", "agent")}),
        ("Raw Sync Data", {"fields": ("schools", "raw_fees", "office_info", "floor_plans")}),
        ("Merchandising", {"fields": ("is_featured", "homepage_featured")}),
    )

    @admin.display(description="Total monthly")
    def total_monthly_display(self, obj):
        if not obj.pk:
            return "—"
        return format_html("<strong>{}</strong>", format_usd(obj.total_monthly_cents))

    @admin.display(description="Verified")
    def staleness(self, obj):
        """
        Manual entry means drift is a staffing problem, not a technical one.
        This makes it visible, which is the difference between a known problem
        and a renter discovering a home was leased three weeks ago.
        """
        from django.utils import timezone

        if not obj.last_verified_at:
            return format_html('<span style="color:#b3261e">never</span>')
        days = (timezone.now() - obj.last_verified_at).days
        colour = "#b3261e" if days > 14 else "#8a5a0b" if days > 7 else "#0b6b47"
        return format_html('<span style="color:{}">{} days ago</span>', colour, days)

    @admin.action(description="Mark verified against reality")
    def mark_verified(self, request, queryset):
        from django.utils import timezone

        updated = queryset.update(last_verified_at=timezone.now())
        self.message_user(request, f"{updated} listing(s) marked verified.")

    @admin.action(description="Publish")
    def publish(self, request, queryset):
        self.message_user(request, f"{queryset.update(is_published=True)} published.")

    @admin.action(description="Unpublish")
    def unpublish(self, request, queryset):
        self.message_user(request, f"{queryset.update(is_published=False)} unpublished.")


@admin.register(AmenityCategory)
class AmenityCategoryAdmin(UnfoldModelAdmin):
    pass

