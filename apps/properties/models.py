"""
Inventory: properties, images, fees, amenities.

MONEY IS INTEGER CENTS. See apps/core/money.py for why this is not DecimalField.

THE SINGLE-PRIMARY-IMAGE RULE IS A DATABASE CONSTRAINT, NOT SIGNAL CODE.

The spec describes it as behaviour ("setting is_primary unsets the others"),
which works until two requests set a primary at once, or a bulk import writes
directly, or someone fixes data in a shell. A conditional UniqueConstraint makes
the second primary impossible rather than unlikely. The service layer still
clears the old flag first — that is what makes the operation succeed — but the
invariant does not depend on it remembering to.
"""

import operator
import re
import uuid
from functools import reduce

from django.core.exceptions import ValidationError
from django.db import models
from django.utils.text import slugify


class PropertyStatus(models.TextChoices):
    AVAILABLE = "available", "Available now"
    COMING_SOON = "coming-soon", "Coming soon"
    APPLICATION_PENDING = "application-pending", "Application pending"
    LEASED = "leased", "Leased"
    OFF_MARKET = "off-market", "Off market"


class PropertyType(models.TextChoices):
    RESIDENTIAL = "residential", "Single-family"
    TOWNHOUSE = "townhouse", "Townhome"
    CONDO = "condo", "Condo"
    APARTMENT = "apartment", "Apartment"


# Statuses a member of the public may see at all.
PUBLIC_STATUSES = [
    PropertyStatus.AVAILABLE, PropertyStatus.COMING_SOON,
    PropertyStatus.APPLICATION_PENDING, PropertyStatus.LEASED,
]
# Statuses that count as live inventory for hub counts and thresholds.
RENTABLE_STATUSES = [PropertyStatus.AVAILABLE, PropertyStatus.COMING_SOON]



"""
FEE ROWS THAT ARE JUST THE RENT AGAIN.

Partner feeds ship a monthly fee row literally labelled "Base Monthly Rent"
whose amount is the rent - 4,565 of them, one on every home in the catalogue.
It is not an additional charge; it is the feed restating the rent it already
sent in `price_cents`. Summing it alongside `price_cents` doubled the advertised
monthly cost on every listing, every card, every map pin and every price filter:
a $2,011.80 home displayed as $4,055.60.

Excluded here rather than at each call site so the annotation, the serializer
and the admin cannot disagree about what a home costs. Matched on the label
rather than on `amount == price_cents`, because 864 of them carry rent plus a
small uplift and would slip through an equality test.
"""
RENT_RESTATEMENT_LABELS = ("base monthly rent", "base rent", "rent", "monthly rent")

RENT_RESTATEMENT = reduce(
    operator.or_,
    (models.Q(label__iexact=label) for label in RENT_RESTATEMENT_LABELS),
)


def is_rent_restatement(label: str) -> bool:
    return (label or "").strip().lower() in RENT_RESTATEMENT_LABELS


_SEARCH_PUNCTUATION = re.compile(r"[^a-z0-9]+")


def normalise_search_text(value: str) -> str:
    """
    Lowercase, punctuation to spaces, whitespace collapsed.

    Used for BOTH the stored haystack and the incoming query, which is the
    only way the two are guaranteed to agree - normalising one side only is
    how "St." stops matching "st".
    """
    return _SEARCH_PUNCTUATION.sub(" ", (value or "").lower()).strip()


class PropertyQuerySet(models.QuerySet):
    def public(self):
        """
        A HOME WITH NO RENT IS NOT AN OFFER.

        Three feed records carry `price_cents = 0`, which renders as "$0/mo
        total" and, under the default price-ascending sort, put them at the top
        of the first page of search. The guard lives here rather than in the
        search view so the sitemap, the map pins and the city counts cannot
        disagree with each other about what is on the market.
        """
        return self.filter(
            is_published=True, status__in=PUBLIC_STATUSES, price_cents__gt=0
        )

    def rentable(self):
        return self.filter(
            is_published=True, status__in=RENTABLE_STATUSES, price_cents__gt=0
        )

    def with_total_monthly(self):
        """
        Annotate the all-in monthly figure.

        A correlated subquery rather than a JOIN with GROUP BY: joining
        multiplies rows when a property has several fees, and the COUNT used for
        pagination is then wrong in a way that only appears once some homes have
        more fees than others.
        """
        from django.db.models import OuterRef, Subquery, Sum, Value
        from django.db.models.functions import Coalesce

        required_monthly = (
            PropertyFee.objects
            .filter(property_id=OuterRef("pk"), cadence=FeeCadence.MONTHLY, condition=FeeCondition.REQUIRED)
            # See RENT_RESTATEMENT: without this the rent is counted twice.
            .exclude(RENT_RESTATEMENT)
            .values("property_id")
            .annotate(total=Sum("amount_cents"))
            .values("total")
        )
        return self.annotate(
            total_monthly_cents=models.F("price_cents")
            + Coalesce(Subquery(required_monthly, output_field=models.BigIntegerField()), Value(0))
        )


class Property(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    title = models.CharField(max_length=200)
    slug = models.SlugField(max_length=250, unique=True, db_index=True)
    description = models.TextField(blank=True, default="")
    type = models.CharField(max_length=20, choices=PropertyType.choices, default=PropertyType.RESIDENTIAL)
    status = models.CharField(
        max_length=24, choices=PropertyStatus.choices, default=PropertyStatus.AVAILABLE, db_index=True,
    )

    price_cents = models.BigIntegerField(db_index=True)
    # Captured once, never rewritten by a price change, so a markdown computed
    # against it cannot compound: re-running an importer that applies "10% off"
    # would otherwise walk the price down every night.
    original_price_cents = models.BigIntegerField(null=True, blank=True)
    price_label = models.CharField(max_length=20, blank=True, default="/mo")

    bedrooms = models.PositiveSmallIntegerField(default=0, db_index=True)
    # Halves exist (2.5), so this is stored doubled as an integer: 5 == 2.5.
    # Avoids a float in a filtered, sorted column.
    half_bathrooms = models.PositiveSmallIntegerField(default=0)
    sqft = models.PositiveIntegerField(default=0)
    year_built = models.PositiveSmallIntegerField(null=True, blank=True)
    garage = models.PositiveSmallIntegerField(default=0)
    stories = models.PositiveSmallIntegerField(default=1)

    address = models.CharField(max_length=200)
    city = models.CharField(max_length=100, db_index=True)
    state = models.CharField(max_length=2, db_index=True)
    zip_code = models.CharField(max_length=10)
    latitude = models.FloatField(null=True, blank=True)
    longitude = models.FloatField(null=True, blank=True)
    neighborhood = models.CharField(max_length=100, blank=True, default="")

    # Everything a renter might type, flattened into one normalised haystack.
    # Denormalised on purpose: matching across five columns means five ORs per
    # token and no index can help any of them, whereas one normalised column
    # is a single scan and stays portable between SQLite and Postgres.
    search_text = models.CharField(max_length=500, blank=True, default="", db_index=True)

    # Not in the supplied spec, and required. Voucher acceptance is a filter, a
    # landing page, and a promise repeated on every page of the public site.
    # Both partner feeds omit it, so it is maintained here.
    voucher_accepted = models.BooleanField(default=False, db_index=True)
    pets_allowed = models.BooleanField(default=False)
    pet_policy = models.TextField(blank=True, default="")
    accessibility_features = models.JSONField(default=list, blank=True)

    # Home detail, shown on the public page. Free text because that is what
    # manual entry produces; the partner feed sends none of it.
    parking = models.CharField(max_length=120, blank=True, default="")
    laundry = models.CharField(max_length=120, blank=True, default="")
    hvac = models.CharField(max_length=120, blank=True, default="")
    flooring = models.CharField(max_length=120, blank=True, default="")
    appliances = models.JSONField(default=list, blank=True)

    # Validated against a provider allowlist before the public site frames
    # them — an embed URL is untrusted input even when staff typed it.
    tour_3d_url = models.URLField(max_length=500, blank=True, default="")
    tour_video_url = models.URLField(max_length=500, blank=True, default="")

    is_featured = models.BooleanField(default=False)
    homepage_featured = models.BooleanField(default=False)
    is_published = models.BooleanField(default=False, db_index=True)

    available_from = models.DateField(null=True, blank=True)
    leased_at = models.DateTimeField(null=True, blank=True)
    # When a person last confirmed the record against reality. Distinct from
    # updated_at, which any automated write moves.
    last_verified_at = models.DateTimeField(null=True, blank=True)

    agent = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.PROTECT, related_name="listings",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = PropertyQuerySet.as_manager()

    class Meta:
        db_table = "properties"
        ordering = ["-created_at"]
        verbose_name = "home"
        verbose_name_plural = "homes"
        indexes = [
            models.Index(fields=["state", "city"]),
            models.Index(fields=["is_published", "status"]),
            models.Index(fields=["latitude", "longitude"]),
        ]
        constraints = [
            # The 45-day grace window during which a leased home's URL keeps
            # working is measured from leased_at. Without it, a long-gone home
            # stays reachable indefinitely.
            models.CheckConstraint(
                condition=models.Q(status="leased", leased_at__isnull=False) | ~models.Q(status="leased"),
                name="leased_property_has_leased_at",
            ),
            models.CheckConstraint(condition=models.Q(price_cents__gte=0), name="price_not_negative"),
        ]

    def __str__(self) -> str:
        return f"{self.address}, {self.city} {self.state}"

    @property
    def bathrooms(self) -> float:
        return self.half_bathrooms / 2

    def build_slug(self) -> str:
        """
        Address-based, not title-based.

        A title is edited freely, and a slug that moves breaks every inbound
        link and saved tab.
        """
        base = slugify(f"{self.address} {self.city} {self.state} {self.zip_code}") or "property"
        if not base.startswith("srg-"):
            return f"srg-{base}"
        return base

    def build_search_text(self) -> str:
        """
        The haystack: address, city, state, ZIP and neighbourhood, normalised.

        Punctuation is stripped rather than kept so that "5445 Verdugos Pl."
        and "verdugos pl" are the same string to match against, and so a
        trailing comma in a feed record cannot break a match.
        """
        return normalise_search_text(
            " ".join(
                part
                for part in (
                    self.address, self.city, self.state,
                    self.zip_code, self.neighborhood,
                )
                if part
            )
        )

    def save(self, *args, **kwargs):
        self.state = (self.state or "").upper()
        self.search_text = self.build_search_text()
        if not self.slug:
            base = self.build_slug()
            candidate, n = base, 2
            while Property.objects.filter(slug=candidate).exclude(pk=self.pk).exists():
                candidate, n = f"{base}-{n}", n + 1
            self.slug = candidate
        if self.original_price_cents is None:
            self.original_price_cents = self.price_cents
        super().save(*args, **kwargs)

    def apply_discount(self, basis_points: int) -> int:
        """Discount off the ORIGINAL price, so applying it twice is a no-op."""
        from apps.core.money import basis_points_of

        base = self.original_price_cents or self.price_cents
        self.price_cents = base - basis_points_of(base, basis_points)
        self.save(update_fields=["price_cents", "updated_at"])
        return self.price_cents

    @property
    def total_monthly_cents(self) -> int:
        """
        Base rent plus every REQUIRED monthly fee. Never base rent alone.

        Shares its name with the `with_total_monthly()` annotation on purpose,
        so callers write one thing whether they are filtering a queryset or
        reading a single instance. Django assigns annotations onto the instance,
        which needs the setter below — without it the two collide with
        "property has no setter" the moment anyone annotates.
        """
        cached = getattr(self, "_total_monthly_cents", None)
        if cached is not None:
            return cached
        required = self.fees.filter(
            cadence=FeeCadence.MONTHLY, condition=FeeCondition.REQUIRED,
        ).aggregate(total=models.Sum("amount_cents"))["total"] or 0
        return self.price_cents + required

    @total_monthly_cents.setter
    def total_monthly_cents(self, value: int) -> None:
        self._total_monthly_cents = value


class PropertyImage(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    property = models.ForeignKey(Property, on_delete=models.CASCADE, related_name="images")
    # What actually renders — a URL on infrastructure we control.
    url = models.CharField(max_length=500)
    # Where it came from, when ingested from a partner feed. Kept so re-ingest
    # is possible; never rendered, because serving a partner CDN URL makes the
    # whole catalogue depend on infrastructure we do not control.
    source_url = models.URLField(max_length=500, blank=True, default="")
    caption = models.CharField(max_length=200, blank=True, default="")
    is_primary = models.BooleanField(default=False)
    sort_order = models.PositiveSmallIntegerField(default=0)
    width = models.PositiveIntegerField(null=True, blank=True)
    height = models.PositiveIntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "property_images"
        ordering = ["-is_primary", "sort_order", "created_at"]
        constraints = [
            # See the module docstring: the guarantee, not the mechanism.
            models.UniqueConstraint(
                fields=["property"], condition=models.Q(is_primary=True),
                name="one_primary_image_per_property",
            ),
        ]

    def __str__(self) -> str:
        return f"Image for {self.property_id}"

    def make_primary(self) -> None:
        """Clearing first is what lets the write succeed; both in one atomic block."""
        from django.db import transaction

        with transaction.atomic():
            PropertyImage.objects.filter(property=self.property, is_primary=True).exclude(pk=self.pk).update(
                is_primary=False,
            )
            self.is_primary = True
            self.save(update_fields=["is_primary"])

    def delete(self, *args, **kwargs):
        """
        Promote a replacement when the primary is removed.

        Without this the property silently loses its card photo: invisible in
        the admin, obvious on the search page.
        """
        from django.db import transaction

        was_primary, property_id = self.is_primary, self.property_id
        with transaction.atomic():
            result = super().delete(*args, **kwargs)
            if was_primary:
                nxt = PropertyImage.objects.filter(property_id=property_id).order_by(
                    "sort_order", "created_at",
                ).first()
                if nxt:
                    PropertyImage.objects.filter(pk=nxt.pk).update(is_primary=True)
        return result


class FeeCadence(models.TextChoices):
    MONTHLY = "monthly", "Monthly"
    ONE_TIME = "one-time", "One time"


class FeeCondition(models.TextChoices):
    REQUIRED = "required", "Required"
    CONDITIONAL = "conditional", "Conditional"


class PropertyFee(models.Model):
    """
    The table the pricing promise rests on.

    The public site never advertises a base rent: every surface shows base rent
    plus every required monthly fee, and the itemised lines must sum exactly to
    the displayed total. Search price filters compare against that total, so a
    renter capping at $2,000 is never shown a home that costs $2,150 to live in.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    property = models.ForeignKey(Property, on_delete=models.CASCADE, related_name="fees")
    fee_key = models.SlugField(max_length=50)
    label = models.CharField(max_length=120)
    amount_cents = models.BigIntegerField()
    cadence = models.CharField(max_length=10, choices=FeeCadence.choices, default=FeeCadence.MONTHLY)
    condition = models.CharField(max_length=12, choices=FeeCondition.choices, default=FeeCondition.REQUIRED)
    # An unexplained line item on a fee table reads as padding.
    reason = models.TextField(blank=True, default="")
    # Shown when conditional, e.g. "if you have a pet".
    applies_when = models.CharField(max_length=200, blank=True, default="")
    sort_order = models.PositiveSmallIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "property_fees"
        ordering = ["sort_order", "label"]
        constraints = [
            models.UniqueConstraint(fields=["property", "fee_key"], name="unique_fee_key_per_property"),
            models.CheckConstraint(condition=models.Q(amount_cents__gte=0), name="fee_not_negative"),
            # A conditional fee must say what triggers it, or the breakdown
            # shows an amount the renter cannot tell whether they will be
            # charged.
            models.CheckConstraint(
                condition=~models.Q(condition="conditional") | ~models.Q(applies_when=""),
                name="conditional_fee_states_when",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.label} ({self.amount_cents}c {self.cadence})"

    def clean(self):
        if self.condition == FeeCondition.CONDITIONAL and not self.applies_when.strip():
            raise ValidationError({"applies_when": "A conditional fee must state when it applies."})


class AmenityCategory(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=80, unique=True)
    icon = models.CharField(max_length=40, blank=True, default="")
    sort_order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        db_table = "amenity_categories"
        ordering = ["sort_order", "name"]
        verbose_name_plural = "amenity categories"

    def __str__(self) -> str:
        return self.name


class PropertyAmenity(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    property = models.ForeignKey(Property, on_delete=models.CASCADE, related_name="amenities")
    category = models.ForeignKey(AmenityCategory, null=True, blank=True, on_delete=models.SET_NULL)
    name = models.CharField(max_length=120)
    slug = models.SlugField(max_length=120)

    class Meta:
        db_table = "property_amenities"
        constraints = [
            models.UniqueConstraint(fields=["property", "slug"], name="unique_amenity_per_property"),
        ]
        verbose_name_plural = "property amenities"

    def __str__(self) -> str:
        return self.name


class FavoriteProperty(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey("accounts.User", on_delete=models.CASCADE, related_name="favorites")
    property = models.ForeignKey(Property, on_delete=models.CASCADE, related_name="favorited_by")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "favorite_properties"
        constraints = [
            models.UniqueConstraint(fields=["user", "property"], name="unique_favorite"),
        ]
        verbose_name_plural = "favorite properties"
