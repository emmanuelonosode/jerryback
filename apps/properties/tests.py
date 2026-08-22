from django.db import IntegrityError, transaction
from django.test import TestCase

from apps.core.money import dollars
from .models import (
    FeeCadence, FeeCondition, Property, PropertyFee, PropertyImage, PropertyStatus,
)


def make_property(**over):
    defaults = dict(
        title="Three bedroom home", price_cents=dollars(1200), address="8043 Kenilworth St",
        city="Charlotte", state="nc", zip_code="28203", bedrooms=3, half_bathrooms=5,
        latitude=35.2271, longitude=-80.8431, is_published=True,
    )
    return Property.objects.create(**{**defaults, **over})


class SlugTests(TestCase):
    def test_slug_comes_from_the_address_not_the_title(self):
        # A title is edited freely; a slug that moves breaks every inbound link.
        home = make_property()
        self.assertEqual(home.slug, "srg-8043-kenilworth-st-charlotte-nc-28203")

    def test_collisions_get_a_numeric_suffix_not_a_uuid(self):
        make_property()
        second = make_property()
        self.assertEqual(second.slug, "srg-8043-kenilworth-st-charlotte-nc-28203-2")

    def test_state_is_upper_cased_because_it_drives_the_licence_shown(self):
        self.assertEqual(make_property(state="nc").state, "NC")


class PricingTests(TestCase):
    def test_original_price_is_captured_on_insert(self):
        self.assertEqual(make_property(price_cents=dollars(1200)).original_price_cents, dollars(1200))

    def test_a_price_change_does_not_move_the_original(self):
        home = make_property(price_cents=dollars(1200))
        home.price_cents = dollars(1100)
        home.save()
        home.refresh_from_db()
        self.assertEqual(home.price_cents, dollars(1100))
        self.assertEqual(home.original_price_cents, dollars(1200))

    def test_discounts_do_not_compound_when_applied_twice(self):
        # The failure this prevents: a nightly importer re-applying "10% off"
        # walks the price down every run.
        home = make_property(price_cents=dollars(1000))
        self.assertEqual(home.apply_discount(1000), dollars(900))
        self.assertEqual(home.apply_discount(1000), dollars(900))

    def test_total_monthly_is_rent_plus_required_monthly_fees(self):
        home = make_property(price_cents=dollars(1200))
        PropertyFee.objects.create(
            property=home, fee_key="internet", label="Internet", amount_cents=dollars(85),
        )
        self.assertEqual(home.total_monthly_cents, dollars(1285))

    def test_conditional_and_one_time_fees_are_excluded_from_the_monthly_total(self):
        # A pet fee is not paid by everyone, so it must not push a home out of a
        # budget a renter without a pet would meet.
        home = make_property(price_cents=dollars(1200))
        PropertyFee.objects.create(
            property=home, fee_key="pet", label="Pet", amount_cents=dollars(200),
            condition=FeeCondition.CONDITIONAL, applies_when="if you have a pet",
        )
        PropertyFee.objects.create(
            property=home, fee_key="admin", label="Admin", amount_cents=dollars(150),
            cadence=FeeCadence.ONE_TIME,
        )
        self.assertEqual(home.total_monthly_cents, dollars(1200))

    def test_a_conditional_fee_must_state_when_it_applies(self):
        # Otherwise the breakdown shows an amount the renter cannot tell whether
        # they will be charged.
        home = make_property()
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                PropertyFee.objects.create(
                    property=home, fee_key="pet", label="Pet", amount_cents=dollars(200),
                    condition=FeeCondition.CONDITIONAL, applies_when="",
                )


class PrimaryImageTests(TestCase):
    def setUp(self):
        self.home = make_property()

    def test_the_database_refuses_a_second_primary(self):
        # Service code that "unsets the others first" is correct until two
        # requests race, an import writes directly, or someone edits by hand.
        PropertyImage.objects.create(property=self.home, url="/a.jpg", is_primary=True)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                PropertyImage.objects.create(property=self.home, url="/b.jpg", is_primary=True)

    def test_any_number_of_non_primary_images_is_fine(self):
        for i in range(3):
            PropertyImage.objects.create(property=self.home, url=f"/{i}.jpg")
        self.assertEqual(self.home.images.count(), 3)

    def test_two_properties_may_each_have_a_primary(self):
        other = make_property(address="99 Other St")
        PropertyImage.objects.create(property=self.home, url="/a.jpg", is_primary=True)
        PropertyImage.objects.create(property=other, url="/b.jpg", is_primary=True)
        self.assertEqual(PropertyImage.objects.filter(is_primary=True).count(), 2)

    def test_make_primary_demotes_the_incumbent(self):
        first = PropertyImage.objects.create(property=self.home, url="/a.jpg", is_primary=True)
        second = PropertyImage.objects.create(property=self.home, url="/b.jpg")
        second.make_primary()
        first.refresh_from_db()
        self.assertFalse(first.is_primary)
        self.assertEqual(self.home.images.filter(is_primary=True).count(), 1)

    def test_deleting_the_primary_promotes_a_replacement(self):
        # Otherwise the property silently loses its card photo: invisible in the
        # admin, obvious on the search page.
        first = PropertyImage.objects.create(property=self.home, url="/a.jpg", is_primary=True, sort_order=0)
        PropertyImage.objects.create(property=self.home, url="/b.jpg", sort_order=1)
        first.delete()
        remaining = self.home.images.get()
        self.assertTrue(remaining.is_primary)

    def test_the_partner_cdn_url_is_kept_separate_from_what_renders(self):
        image = PropertyImage.objects.create(
            property=self.home, url="/ingested/a.avif", source_url="https://cdn.partner.example/a.jpg",
        )
        self.assertEqual(image.url, "/ingested/a.avif")
        self.assertNotEqual(image.url, image.source_url)


class LeasedTests(TestCase):
    def test_a_leased_property_must_record_when(self):
        # The 45-day grace window during which the URL keeps working is measured
        # from this date; without it a long-gone home stays reachable forever.
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                make_property(status=PropertyStatus.LEASED, leased_at=None)


class QuerySetTests(TestCase):
    def test_public_excludes_unpublished(self):
        make_property(address="1 Draft St", is_published=False)
        self.assertEqual(Property.objects.public().count(), 0)
        self.assertEqual(Property.objects.count(), 1)

    def test_rentable_excludes_leased(self):
        from django.utils import timezone

        make_property(address="1 A St")
        make_property(address="2 B St", status=PropertyStatus.LEASED, leased_at=timezone.now())
        self.assertEqual(Property.objects.rentable().count(), 1)

    def test_with_total_monthly_annotates_the_all_in_figure(self):
        # Price filters compare against this, never base rent: a renter capping
        # at $2,000 must not be shown a home that costs $2,150 to live in.
        cheap = make_property(address="1 Cheap St", price_cents=dollars(1900))
        PropertyFee.objects.create(property=cheap, fee_key="i", label="Internet", amount_cents=dollars(85))
        dear = make_property(address="2 Dear St", price_cents=dollars(1950))
        PropertyFee.objects.create(property=dear, fee_key="i", label="Internet", amount_cents=dollars(200))

        within = Property.objects.public().with_total_monthly().filter(
            total_monthly_cents__lte=dollars(2000),
        )
        self.assertEqual([p.id for p in within], [cheap.id])

    def test_pagination_counts_rows_not_fee_joined_duplicates(self):
        # A JOIN instead of a correlated subquery multiplies rows per fee, and
        # the count is then wrong only for homes with more fees than others.
        home = make_property()
        for key in ("a", "b", "c"):
            PropertyFee.objects.create(property=home, fee_key=key, label=key, amount_cents=dollars(10))
        self.assertEqual(Property.objects.public().with_total_monthly().count(), 1)


class VoucherTests(TestCase):
    def test_voucher_acceptance_is_a_first_class_field(self):
        # Absent from the supplied spec and from both partner feeds. It is a
        # filter, a landing page, and a promise repeated on every public page.
        accepting = make_property(address="1 V St", voucher_accepted=True)
        make_property(address="2 N St", voucher_accepted=False)
        self.assertEqual(
            [p.id for p in Property.objects.filter(voucher_accepted=True)], [accepting.id],
        )


class InventorySearchTests(TestCase):
    """
    The search endpoint now carries filtering, sorting and paging that used to
    happen in the site's memory over a 200-record slice of the catalogue.
    """

    def setUp(self):
        self.cheap = make_property(
            address="1 Low St", price_cents=dollars(900), bedrooms=2, half_bathrooms=2
        )
        self.mid = make_property(
            address="2 Mid St", price_cents=dollars(1500), bedrooms=3, half_bathrooms=4
        )
        self.dear = make_property(
            address="3 High St", price_cents=dollars(2500), bedrooms=5, half_bathrooms=6
        )

    def _addresses(self, **params):
        response = self.client.get("/api/v1/properties/", params)
        self.assertEqual(response.status_code, 200)
        return [row["address"] for row in response.json()["results"]]

    def test_price_ascending_orders_by_the_all_in_total(self):
        self.assertEqual(
            self._addresses(sort="price-asc"), ["1 Low St", "2 Mid St", "3 High St"]
        )

    def test_price_descending_reverses_it(self):
        self.assertEqual(
            self._addresses(sort="price-desc"), ["3 High St", "2 Mid St", "1 Low St"]
        )

    def test_most_bedrooms_first(self):
        self.assertEqual(self._addresses(sort="beds-desc")[0], "3 High St")

    def test_an_unknown_sort_falls_back_rather_than_erroring(self):
        response = self.client.get("/api/v1/properties/", {"sort": "'; DROP TABLE"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["count"], 3)

    def test_minimum_bathrooms_compares_against_whole_baths(self):
        # `bathrooms` is derived as half_bathrooms / 2, so 2+ baths means 4+ halves.
        self.assertEqual(
            sorted(self._addresses(min_bathrooms=2)), ["2 Mid St", "3 High St"]
        )

    def test_a_home_with_no_availability_date_is_available_now(self):
        # Excluding nulls here would hide most of the catalogue behind a date.
        self.assertEqual(len(self._addresses(available_by="2030-01-01")), 3)

    def test_availability_excludes_homes_that_free_up_later(self):
        self.mid.available_from = "2031-06-01"
        self.mid.save(update_fields=["available_from"])
        self.assertNotIn("2 Mid St", self._addresses(available_by="2030-01-01"))

    def test_paging_does_not_repeat_or_drop_a_home(self):
        first = self._addresses(sort="price-asc", page=1, page_size=2)
        second = self._addresses(sort="price-asc", page=2, page_size=2)
        self.assertEqual(len(first), 2)
        self.assertEqual(len(second), 1)
        self.assertEqual(len(set(first + second)), 3)


class UnpricedHomeTests(TestCase):
    def test_a_home_with_no_rent_is_not_public(self):
        # It rendered as "$0/mo total" and, under the default price-ascending
        # sort, led the first page of search.
        make_property(address="4 Free St", price_cents=0)
        self.assertNotIn(
            "4 Free St", [p.address for p in Property.objects.public()]
        )

    def test_a_home_with_no_rent_is_not_rentable_either(self):
        make_property(address="5 Free St", price_cents=0)
        self.assertNotIn(
            "5 Free St", [p.address for p in Property.objects.rentable()]
        )


class FreeTextSearchTests(TestCase):
    """
    The search box used to be a `city__iexact` filter, so a street name, a
    house number or a ZIP returned nothing at all.
    """

    def setUp(self):
        self.a = make_property(
            address="5445 Verdugos Pl", city="San Antonio", state="tx", zip_code="78244"
        )
        self.b = make_property(
            address="1465 Lake Lucerne Rd SW", city="Lilburn", state="ga", zip_code="30047"
        )
        self.c = make_property(
            address="22 Oak St", city="Lilburn", state="ga", zip_code="30047"
        )

    def find(self, q, **extra):
        response = self.client.get("/api/v1/properties/", {"q": q, **extra})
        self.assertEqual(response.status_code, 200)
        return [r["address"] for r in response.json()["results"]]

    def test_finds_a_street_name(self):
        self.assertEqual(self.find("verdugos"), ["5445 Verdugos Pl"])

    def test_finds_a_full_address(self):
        self.assertEqual(self.find("5445 Verdugos Pl"), ["5445 Verdugos Pl"])

    def test_finds_by_zip(self):
        self.assertEqual(self.find("78244"), ["5445 Verdugos Pl"])

    def test_finds_by_city(self):
        self.assertEqual(sorted(self.find("lilburn")), ["1465 Lake Lucerne Rd SW", "22 Oak St"])

    def test_punctuation_and_case_do_not_matter(self):
        # A feed record with a trailing period must not stop matching.
        self.assertEqual(self.find("  VERDUGOS,  pl. "), ["5445 Verdugos Pl"])

    def test_every_token_must_match(self):
        # OR would return both Lilburn homes here, which reads as broken.
        self.assertEqual(self.find("lake lilburn"), ["1465 Lake Lucerne Rd SW"])

    def test_a_full_address_outranks_a_partial_token_match(self):
        results = self.find("22 oak")
        self.assertEqual(results[0], "22 Oak St")

    def test_one_wrong_word_still_finds_the_home(self):
        # "crt" matches nothing; the OR fallback keeps the result rather than
        # showing an empty page.
        self.assertIn("5445 Verdugos Pl", self.find("verdugos crt san antonio"))

    def test_nonsense_returns_nothing_rather_than_everything(self):
        self.assertEqual(self.find("zzzznotathing"), [])

    def test_search_combines_with_the_other_filters(self):
        self.assertEqual(self.find("lilburn", min_bedrooms=99), [])

    def test_an_explicit_sort_still_wins_over_relevance(self):
        cheap = make_property(
            address="9 Oak St", city="Lilburn", state="ga", zip_code="30047",
            price_cents=dollars(100),
        )
        self.assertEqual(self.find("oak", sort="price-asc")[0], cheap.address)

    def test_bulk_written_rows_are_searchable_after_a_rebuild(self):
        # bulk_create bypasses save(), so the haystack would be empty and the
        # home invisible to search while visible to every filter.
        Property.objects.bulk_create([
            Property(
                title="Bulk", address="77 Hidden Way", city="Lilburn", state="GA",
                zip_code="30047", price_cents=dollars(1000), bedrooms=3, half_bathrooms=4,
                is_published=True, slug="bulk-hidden-way",
            )
        ])
        self.assertEqual(self.find("hidden way"), [])
        from django.core.management import call_command
        call_command("rebuild_search_index", verbosity=0)
        self.assertEqual(self.find("hidden way"), ["77 Hidden Way"])


class RentRestatementFeeTests(TestCase):
    """
    Partner feeds send a monthly fee row labelled "Base Monthly Rent" whose
    amount is the rent. Counting it as a fee doubled the advertised monthly
    cost on all 4,556 published homes.
    """

    def setUp(self):
        self.home = make_property(price_cents=dollars(2000))
        PropertyFee.objects.create(
            property=self.home, fee_key="base-monthly-rent", label="Base Monthly Rent",
            amount_cents=dollars(2000), cadence=FeeCadence.MONTHLY,
            condition=FeeCondition.REQUIRED,
        )
        PropertyFee.objects.create(
            property=self.home, fee_key="smart-home", label="Smart Home Keyless Access",
            amount_cents=dollars(20), cadence=FeeCadence.MONTHLY,
            condition=FeeCondition.REQUIRED,
        )

    def total(self):
        return Property.objects.with_total_monthly().get(pk=self.home.pk).total_monthly_cents

    def test_the_rent_is_not_counted_twice(self):
        self.assertEqual(self.total(), dollars(2020))

    def test_a_restatement_carrying_an_uplift_is_still_excluded(self):
        # 864 rows carry rent plus a small percentage, so an equality test on
        # the amount would let them through.
        self.home.fees.filter(label="Base Monthly Rent").update(amount_cents=dollars(2073))
        self.assertEqual(self.total(), dollars(2020))

    def test_the_itemised_lines_sum_to_the_advertised_total(self):
        response = self.client.get(f"/api/v1/properties/{self.home.slug}/")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        monthly = sum(
            f["amount_cents"] for f in body["fees"]
            if f["cadence"] == "monthly" and f["condition"] == "required"
        )
        self.assertEqual(body["price_cents"] + monthly, self.total())

    def test_the_duplicate_line_is_not_shown_to_a_renter(self):
        body = self.client.get(f"/api/v1/properties/{self.home.slug}/").json()
        self.assertNotIn("Base Monthly Rent", [f["label"] for f in body["fees"]])

    def test_a_genuine_fee_is_still_counted(self):
        self.assertIn(
            "Smart Home Keyless Access",
            [f["label"] for f in self.client.get(f"/api/v1/properties/{self.home.slug}/").json()["fees"]],
        )

    def test_price_filters_compare_against_the_corrected_total(self):
        # The renter capping at $2,050 must see this home; before the fix its
        # total read $4,020 and it was filtered out.
        response = self.client.get("/api/v1/properties/", {"max_price_cents": dollars(2050)})
        self.assertIn(self.home.slug, [r["slug"] for r in response.json()["results"]])
