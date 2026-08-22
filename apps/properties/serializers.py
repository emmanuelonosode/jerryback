"""Saved-property serializers for the resident portal."""

from rest_framework import serializers

from .models import FavoriteProperty, Property


class FavoritePropertySummarySerializer(serializers.ModelSerializer):
    bathrooms = serializers.FloatField(read_only=True)
    primary_image_url = serializers.SerializerMethodField()
    full_address = serializers.SerializerMethodField()

    class Meta:
        model = Property
        fields = [
            "id", "slug", "title", "address", "city", "state", "zip_code",
            "full_address", "bedrooms", "bathrooms", "sqft",
            "price_cents", "price_label", "status", "primary_image_url",
        ]
        read_only_fields = fields

    def get_primary_image_url(self, obj) -> str | None:
        image = obj.images.first()
        return absolute_media_url(image.url, self.context.get("request")) if image else None

    def get_full_address(self, obj) -> str:
        return f"{obj.address}, {obj.city}, {obj.state} {obj.zip_code}".strip()


class FavoriteSerializer(serializers.ModelSerializer):
    property = FavoritePropertySummarySerializer(read_only=True)

    class Meta:
        model = FavoriteProperty
        fields = ["id", "property", "created_at"]
        read_only_fields = fields

# ===========================================================================
# Public inventory
#
# THE API ADVERTISES THE TOTAL, NOT BASE RENT. `total_monthly_cents` is rent
# plus every required monthly fee, annotated by `with_total_monthly()`. It is
# the number the whole brand position rests on, so it is computed in the
# database rather than assembled per row in Python, and the price filter
# compares against it — a renter capping at $2,000 must never be shown a home
# that costs $2,150 to live in.
#
# THE PARTNER CDN SOURCE IS NEVER SERIALISED. `PropertyImage.source_url` points
# at infrastructure nobody here controls. A client that can see it will
# eventually render it, and then the catalogue depends on that host staying up.
# ===========================================================================

from django.conf import settings  # noqa: E402

from apps.core.money import format_usd  # noqa: E402


def absolute_media_url(url: str, request) -> str:
    """Make a stored media path usable from another origin."""
    if not url or url.startswith(("http://", "https://", "//")):
        return url

    base = getattr(settings, "MEDIA_BASE_URL", "") or ""
    if base:
        return f"{base.rstrip('/')}/{url.lstrip('/')}"
    if request is not None:
        return request.build_absolute_uri(url)
    return url

from .models import PropertyAmenity, PropertyFee, PropertyImage, is_rent_restatement  # noqa: E402


class PublicImageSerializer(serializers.ModelSerializer):
    """
    Image rows for the public catalogue.

    URLS ARE ABSOLUTE. `PropertyImage.url` is stored relative (`/media/...`)
    because that is what the admin needs, but this API is consumed by a site on
    a DIFFERENT ORIGIN — the admin is admin.<domain>, the site is <domain> — so
    a relative path resolves against the site and 404s. Every photograph then
    falls back to placeholder artwork, which reads to a visitor as "these
    listings are fake" on a site whose whole position is that they are not.

    `MEDIA_BASE_URL` wins when set, for a CDN or a separate media host.
    Otherwise the URL is built from the incoming request, which keeps local
    development working without configuration.
    """

    url = serializers.SerializerMethodField()

    class Meta:
        model = PropertyImage
        fields = ["id", "url", "caption", "is_primary", "sort_order", "width", "height"]
        read_only_fields = fields

    def get_url(self, obj) -> str:
        return absolute_media_url(obj.url, self.context.get("request"))


class PublicFeeSerializer(serializers.ModelSerializer):
    class Meta:
        model = PropertyFee
        fields = [
            "fee_key", "label", "amount_cents", "cadence", "condition",
            "reason", "applies_when",
        ]
        read_only_fields = fields


class PublicAmenitySerializer(serializers.ModelSerializer):
    class Meta:
        model = PropertyAmenity
        fields = ["name", "slug"]
        read_only_fields = fields


class PublicPropertyListSerializer(serializers.ModelSerializer):
    bathrooms = serializers.FloatField(read_only=True)
    total_monthly_cents = serializers.IntegerField(read_only=True)
    total_monthly_display = serializers.SerializerMethodField()
    primary_image = serializers.SerializerMethodField()

    class Meta:
        model = Property
        fields = [
            "id", "slug", "title", "status",
            "address", "city", "state", "zip_code",
            "latitude", "longitude",
            "bedrooms", "bathrooms", "sqft",
            "price_cents", "total_monthly_cents", "total_monthly_display",
            "voucher_accepted", "pets_allowed", "available_from",
            "primary_image",
        ]
        read_only_fields = fields

    def get_total_monthly_display(self, obj) -> str:
        return format_usd(obj.total_monthly_cents)

    def get_primary_image(self, obj):
        # `PropertyImage` orders primary-first, so the first row is the hero.
        image = next(iter(obj.images.all()), None)
        # Context must be forwarded, or the nested serializer has no request
        # and silently falls back to the relative path this exists to fix.
        return PublicImageSerializer(image, context=self.context).data if image else None


class PublicPropertyDetailSerializer(PublicPropertyListSerializer):
    images = PublicImageSerializer(many=True, read_only=True)
    fees = serializers.SerializerMethodField()

    def get_fees(self, obj):
        """
        The itemised lines, minus the feed's restatement of the rent.

        The breakdown has to sum exactly to the advertised total, and the total
        excludes these rows - see RENT_RESTATEMENT in the model. Leaving them in
        here would print "Base rent" and "Base Monthly Rent" as two lines of the
        same money, which is what the detail page was doing.
        """
        return PublicFeeSerializer(
            [fee for fee in obj.fees.all() if not is_rent_restatement(fee.label)],
            many=True,
        ).data
    amenities = PublicAmenitySerializer(many=True, read_only=True)

    class Meta(PublicPropertyListSerializer.Meta):
        fields = [
            *PublicPropertyListSerializer.Meta.fields,
            "description", "type", "year_built", "neighborhood",
            "pet_policy", "accessibility_features",
            "parking", "laundry", "hvac", "flooring", "appliances",
            "tour_3d_url", "tour_video_url", "last_verified_at",
            "images", "fees", "amenities",
        ]
        read_only_fields = fields


class MapPinSerializer(serializers.ModelSerializer):
    total_monthly_cents = serializers.IntegerField(read_only=True)

    class Meta:
        model = Property
        fields = ["id", "slug", "latitude", "longitude", "bedrooms", "total_monthly_cents"]
        read_only_fields = fields
