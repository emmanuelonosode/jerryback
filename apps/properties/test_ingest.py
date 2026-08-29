"""
The two rules applied to every property before it reaches the database.

Both are business decisions with consequences - one is what we are allowed to
say, the other is what we charge - and both are applied to 4,600+ records by a
job that runs unattended at 03:20. Nobody reads that output. These are the
check.
"""

from django.test import SimpleTestCase

from apps.properties.ingest import (
    BRAND,
    DISCOUNT_BASIS_POINTS,
    clean_description,
    clean_text,
    discounted_rent,
    mentions_foreign_brand,
)


class BrandCleaningTests(SimpleTestCase):
    def test_partner_name_becomes_ours(self):
        out = clean_description("Invitation Homes is pleased to present this home.")
        self.assertEqual(out, f"{BRAND} is pleased to present this home.")

    def test_legal_suffixes_go_with_the_name(self):
        # "Invitation Homes, LLC" left a dangling ", LLC" when only the name
        # was replaced.
        for raw in ("Invitation Homes, LLC", "Prime Family Housing LLC"):
            self.assertEqual(clean_text(raw), BRAND, raw)
        # A trailing full stop belongs to the sentence, not to the company
        # name, so it survives - "Skelton Realty Group." is right here.
        self.assertEqual(clean_text("Invitation Homes Inc."), f"{BRAND}.")

    def test_urls_are_substituted_not_deleted(self):
        # Deleting the URL left "Contact us at for details." - grammatical
        # damage in copy a renter reads.
        out = clean_description("Contact us at https://www.invitationhomes.com/x for details.")
        self.assertIn("skeltonrealtygroup.com", out)
        self.assertNotIn("invitationhomes", out)
        self.assertNotIn(" at for ", out)

    def test_bare_domains_are_caught(self):
        self.assertIn("skeltonrealtygroup.com", clean_description("See invitationhomes.com today"))

    def test_markup_is_stripped_not_rendered(self):
        # The feed ships HTML. The frontend prints these as text, and passing a
        # partner's markup through would be an injection hole with extra steps.
        # Tags become spaces, and the space a stripped tag leaves in front of
        # punctuation is closed up again - "Lovely home ." is not shippable.
        self.assertEqual(clean_description("<p>Lovely <b>home</b>.</p>"), "Lovely home.")
        self.assertEqual(clean_description("<script>alert(1)</script>Nice."), "alert(1) Nice.")

    def test_innocent_copy_is_left_alone(self):
        # NOT A REWRITE. A sentence naming nobody must survive untouched.
        original = "A quiet street with mature trees and a fenced yard."
        self.assertEqual(clean_description(original), original)

    def test_similar_words_are_not_collateral(self):
        for safe in ("An invitational tournament nearby.", "Prime location.", "Housing vouchers accepted."):
            self.assertEqual(clean_description(safe), safe, safe)

    def test_empty_input(self):
        self.assertEqual(clean_description(None), "")
        self.assertEqual(clean_text(None), "")

    def test_detector_agrees_with_the_cleaner(self):
        # If `mentions_foreign_brand` can still find something after cleaning,
        # the audit and the cleaner disagree and one of them is lying.
        for raw in (
            "Invitation Homes, LLC manages this home. Visit invitationhomes.com.",
            "Prime Family Housing presents https://primefamilyhousing.com/listing/9",
            "IH Merger Sub LLC is the owner of record.",
        ):
            self.assertFalse(mentions_foreign_brand(clean_description(raw)), raw)


class DiscountTests(SimpleTestCase):
    def test_exactly_twenty_percent(self):
        self.assertEqual(DISCOUNT_BASIS_POINTS, 2000)
        self.assertEqual(discounted_rent(284_500), 227_600)
        self.assertEqual(discounted_rent(222_500), 178_000)

    def test_idempotent_because_it_works_off_the_original(self):
        # The whole reason the discount is defined against `original_price_cents`
        # and never against the current price: a second run must not take 20%
        # off an already-discounted number and land on 36%.
        original = 179_500
        once = discounted_rent(original)
        self.assertEqual(discounted_rent(original), once)
        self.assertNotEqual(discounted_rent(once), once)

    def test_rounds_to_a_whole_cent(self):
        # Rents that do not divide cleanly must still produce an integer, or
        # the itemised breakdown stops adding up to the headline.
        for original in (179_501, 100_003, 1, 7):
            self.assertIsInstance(discounted_rent(original), int)

    def test_a_home_with_no_rent_stays_at_zero(self):
        # A home with no price is not an offer, and 20% off nothing must not
        # become a negative rent.
        for empty in (0, None, -1):
            self.assertEqual(discounted_rent(empty), 0)
