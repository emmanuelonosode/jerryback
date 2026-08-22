"""
API-level tests.

These exercise the HTTP boundary — the place where an authorisation mistake
actually becomes an incident.
"""

from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework.test import APIClient

from apps.core.money import dollars
from apps.properties.models import Property, PropertyFee
from .models import EmailVerificationCode, RefreshToken, User

TEST_SECRET = "a-test-jwt-secret-that-is-long-enough-32"
PASSWORD = "correct horse battery staple"


@override_settings(JWT_SECRET=TEST_SECRET, REST_FRAMEWORK={
    "DEFAULT_AUTHENTICATION_CLASSES": ["apps.accounts.authentication.JWTAuthentication"],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAuthenticated"],
})
class AuthFlowTests(TestCase):
    def setUp(self):
        self.client = APIClient()

    def register(self, email="renter@example.com"):
        return self.client.post(reverse("register"), {
            "email": email, "password": PASSWORD, "first_name": "Ada", "last_name": "Lovelace",
        }, format="json")

    def test_registration_queues_a_code_and_does_not_verify_immediately(self):
        from apps.integrations.models import OutboundEmail

        response = self.register()
        self.assertEqual(response.status_code, 202)
        user = User.objects.get()
        self.assertFalse(user.is_email_verified)
        self.assertEqual(OutboundEmail.objects.count(), 1)

    def test_registration_does_not_reveal_that_an_email_exists(self):
        # Returning "already registered" turns this into an enumeration oracle,
        # which is exactly what a credential-stuffing run wants.
        first = self.register()
        second = self.client.post(reverse("register"), {
            "email": "renter@example.com", "password": PASSWORD,
            "first_name": "Someone", "last_name": "Else",
        }, format="json")
        self.assertEqual(first.status_code, second.status_code)
        self.assertEqual(first.json(), second.json())
        self.assertEqual(User.objects.count(), 1)
        self.assertEqual(User.objects.get().first_name, "Ada")

    def test_the_otp_is_never_returned_over_http(self):
        response = self.register()
        self.assertNotIn("code", response.json())
        self.assertNotIn("otp", str(response.json()).lower())

    def test_verify_then_login_issues_a_token_pair(self):
        self.register()
        user = User.objects.get()
        _, code = EmailVerificationCode.issue(user)

        verified = self.client.post(reverse("verify-email"), {
            "email": "renter@example.com", "code": code,
        }, format="json")
        self.assertEqual(verified.status_code, 200)
        self.assertIn("access", verified.json()["tokens"])

        logged_in = self.client.post(reverse("login"), {
            "email": "renter@example.com", "password": PASSWORD,
        }, format="json")
        self.assertEqual(logged_in.status_code, 200)

    def test_an_unverified_user_cannot_log_in(self):
        self.register()
        response = self.client.post(reverse("login"), {
            "email": "renter@example.com", "password": PASSWORD,
        }, format="json")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["reason"], "unverified")

    def test_a_wrong_password_and_an_unknown_account_look_identical(self):
        self.register()
        User.objects.update(is_email_verified=True)
        wrong = self.client.post(reverse("login"), {
            "email": "renter@example.com", "password": "not the password at all",
        }, format="json")
        missing = self.client.post(reverse("login"), {
            "email": "ghost@example.com", "password": PASSWORD,
        }, format="json")
        self.assertEqual(wrong.status_code, missing.status_code)
        self.assertEqual(wrong.json(), missing.json())

    def _verified_login(self):
        self.register()
        User.objects.update(is_email_verified=True)
        return self.client.post(reverse("login"), {
            "email": "renter@example.com", "password": PASSWORD,
        }, format="json").json()["tokens"]

    def test_refresh_rotates_and_the_old_token_dies(self):
        tokens = self._verified_login()
        rotated = self.client.post(reverse("refresh"), {"refresh": tokens["refresh"]}, format="json")
        self.assertEqual(rotated.status_code, 200)
        self.assertNotEqual(rotated.json()["tokens"]["refresh"], tokens["refresh"])

        reuse = self.client.post(reverse("refresh"), {"refresh": tokens["refresh"]}, format="json")
        self.assertEqual(reuse.status_code, 401)

    def test_replay_revokes_the_whole_family(self):
        # Two parties hold a rotated token and there is no way to tell which is
        # the user. Being wrong costs a login; the alternative leaves an
        # attacker with a live session.
        tokens = self._verified_login()
        rotated = self.client.post(reverse("refresh"), {"refresh": tokens["refresh"]}, format="json").json()
        replay = self.client.post(reverse("refresh"), {"refresh": tokens["refresh"]}, format="json")
        self.assertEqual(replay.json()["reason"], "replayed")

        # The legitimate holder's newest token is dead too.
        after = self.client.post(reverse("refresh"), {"refresh": rotated["tokens"]["refresh"]}, format="json")
        self.assertEqual(after.status_code, 401)
        self.assertEqual(RefreshToken.objects.filter(revoked_at__isnull=True).count(), 0)

    def test_an_access_token_cannot_be_used_to_refresh(self):
        tokens = self._verified_login()
        response = self.client.post(reverse("refresh"), {"refresh": tokens["access"]}, format="json")
        self.assertEqual(response.status_code, 401)

    def test_me_requires_authentication(self):
        self.assertEqual(self.client.get(reverse("me")).status_code, 401)

    def test_me_returns_the_permission_grant_so_clients_need_not_hard_code_it(self):
        tokens = self._verified_login()
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {tokens['access']}")
        body = self.client.get(reverse("me")).json()
        self.assertIn("permissions", body)
        # A CLIENT holds none.
        self.assertEqual(body["permissions"], [])

    def test_a_client_cannot_promote_itself_by_patching_its_role(self):
        tokens = self._verified_login()
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {tokens['access']}")
        self.client.patch(reverse("me"), {"role": "ADMIN", "first_name": "Ada"}, format="json")
        self.assertEqual(User.objects.get().role, "CLIENT")

    def test_the_role_is_read_from_the_database_not_trusted_from_the_token(self):
        # A token minted before a demotion must not keep its old privileges for
        # the rest of its four-hour lifetime.
        tokens = self._verified_login()
        User.objects.update(role="ADMIN")
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {tokens['access']}")
        self.assertEqual(self.client.get(reverse("me")).json()["role"], "ADMIN")

    def test_a_deactivated_user_is_rejected_immediately(self):
        tokens = self._verified_login()
        User.objects.update(is_active=False)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {tokens['access']}")
        self.assertEqual(self.client.get(reverse("me")).status_code, 401)


class PublicInventoryApiTests(TestCase):
    def setUp(self):
        self.client = APIClient()

    def make_home(self, **over):
        defaults = dict(
            title="Home", price_cents=dollars(1200), address="1 A St", city="Charlotte",
            state="NC", zip_code="28203", bedrooms=3, is_published=True,
        )
        return Property.objects.create(**{**defaults, **over})

    def test_inventory_is_readable_without_authentication(self):
        self.make_home()
        response = self.client.get("/api/v1/properties/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["count"], 1)

    def test_unpublished_inventory_is_invisible(self):
        self.make_home(is_published=False)
        self.assertEqual(self.client.get("/api/v1/properties/").json()["count"], 0)

    def test_the_api_advertises_the_total_not_base_rent(self):
        home = self.make_home(price_cents=dollars(1200))
        PropertyFee.objects.create(
            property=home, fee_key="internet", label="Internet", amount_cents=dollars(85),
        )
        row = self.client.get("/api/v1/properties/").json()["results"][0]
        self.assertEqual(row["total_monthly_cents"], dollars(1285))
        self.assertEqual(row["total_monthly_display"], "$1,285")

    def test_price_filters_compare_the_all_in_total(self):
        # A renter capping at $2,000 must not be shown a home that costs $2,150
        # to live in.
        cheap = self.make_home(address="1 Cheap St", price_cents=dollars(1900))
        PropertyFee.objects.create(property=cheap, fee_key="i", label="I", amount_cents=dollars(85))
        dear = self.make_home(address="2 Dear St", price_cents=dollars(1950))
        PropertyFee.objects.create(property=dear, fee_key="i", label="I", amount_cents=dollars(200))

        results = self.client.get(
            f"/api/v1/properties/?max_price_cents={dollars(2000)}",
        ).json()["results"]
        self.assertEqual([r["slug"] for r in results], [cheap.slug])

    def test_the_partner_cdn_source_is_never_exposed(self):
        # A client that can see it will eventually render it, which makes the
        # whole catalogue depend on infrastructure we do not control.
        from apps.properties.models import PropertyImage

        home = self.make_home()
        PropertyImage.objects.create(
            property=home, url="/ingested/a.avif", source_url="https://cdn.partner.example/a.jpg",
        )
        body = self.client.get(f"/api/v1/properties/{home.slug}/").json()
        self.assertNotIn("cdn.partner.example", str(body))
        self.assertNotIn("source_url", str(body))

    def test_city_counts_only_live_inventory(self):
        from django.utils import timezone

        self.make_home(address="1 A St")
        self.make_home(address="2 B St", status="leased", leased_at=timezone.now())
        rows = self.client.get("/api/v1/properties/cities/").json()
        self.assertEqual(rows[0]["count"], 1)

    def test_map_pins_require_a_bounding_box(self):
        self.assertEqual(self.client.get("/api/v1/properties/map_pins/").status_code, 400)

    def test_map_pins_return_the_total_cost(self):
        home = self.make_home(latitude=35.2, longitude=-80.8, price_cents=dollars(1200))
        PropertyFee.objects.create(property=home, fee_key="i", label="I", amount_cents=dollars(85))
        pins = self.client.get(
            "/api/v1/properties/map_pins/?north=36&south=34&east=-80&west=-81",
        ).json()
        self.assertEqual(pins[0]["total_monthly_cents"], dollars(1285))
