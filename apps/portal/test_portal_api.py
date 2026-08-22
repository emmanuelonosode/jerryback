"""
Cross-app portal API tests: applications, favourites, password change.

Grouped here rather than split across three apps because they are one surface —
the resident portal — and the property under test is the same in all of them:
the endpoint answers about the caller and nobody else.
"""

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import RefreshToken, Role, User
from apps.crm.models import ApplicationStatus, Client, Lead, RentalApplication
from apps.properties.models import FavoriteProperty
from apps.core.money import dollars
from apps.properties.tests import make_property

TEST_SECRET = "a-test-jwt-secret-that-is-long-enough-32"
PASSWORD = "correct horse battery staple"
API_SETTINGS = {
    "DEFAULT_AUTHENTICATION_CLASSES": ["apps.accounts.authentication.JWTAuthentication"],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAuthenticated"],
}


def make_user(email, first="Ada", last="Lovelace"):
    return User.objects.create_user(
        email=email, password=PASSWORD, first_name=first, last_name=last, role=Role.CLIENT,
    )


class PortalApiTestCase(TestCase):
    def setUp(self):
        self.api = APIClient()
        self.user = make_user("ada@example.com")
        self.other = make_user("grace@example.com", "Grace", "Hopper")

    def auth(self, user):
        from apps.accounts import jwt as jwt_codec

        token = jwt_codec.encode(subject=str(user.pk), role=user.role, token_type="access")
        self.api.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")


@override_settings(JWT_SECRET=TEST_SECRET, REST_FRAMEWORK=API_SETTINGS)
class MyApplicationsTests(PortalApiTestCase):
    def make_application(self, user, **over):
        defaults = dict(
            user=user, property=make_property(price_cents=dollars(1850)),
            status=ApplicationStatus.SUBMITTED,
            application_fee_cents=dollars(55), is_fee_paid=True,
            first_name=user.first_name, last_name=user.last_name, email=user.email,
        )
        return RentalApplication.objects.create(**{**defaults, **over})

    def test_anonymous_callers_are_rejected(self):
        self.assertEqual(self.api.get(reverse("my-applications")).status_code, 401)

    def test_an_applicant_never_sees_another_applicants_application(self):
        self.make_application(self.other)
        mine = self.make_application(self.user)

        self.auth(self.user)
        body = self.api.get(reverse("my-applications")).json()

        self.assertEqual([a["id"] for a in body], [str(mine.id)])

    def test_the_identity_fields_are_not_echoed_back(self):
        """DOB and SSN last four are held but never serialised to the portal."""
        self.make_application(self.user)
        self.auth(self.user)
        body = self.api.get(reverse("my-applications")).json()[0]

        self.assertNotIn("ssn_last4", body)
        self.assertNotIn("date_of_birth", body)

    def test_the_move_in_breakdown_totals_its_own_lines(self):
        self.make_application(self.user)
        self.auth(self.user)
        move_in = self.api.get(reverse("my-applications")).json()[0]["move_in"]

        self.assertIsNotNone(move_in)
        self.assertEqual(
            move_in["total_cents"],
            sum(item["unit_price_cents"] * item["quantity"] for item in move_in["line_items"]),
        )


@override_settings(JWT_SECRET=TEST_SECRET, REST_FRAMEWORK=API_SETTINGS)
class FavouritesTests(PortalApiTestCase):
    def test_a_resident_only_sees_their_own_saved_homes(self):
        FavoriteProperty.objects.create(user=self.other, property=make_property())
        mine = FavoriteProperty.objects.create(user=self.user, property=make_property())

        self.auth(self.user)
        body = self.api.get(reverse("favorites")).json()

        self.assertEqual([f["id"] for f in body], [str(mine.id)])

    def test_a_resident_cannot_delete_someone_elses_saved_home(self):
        theirs = FavoriteProperty.objects.create(user=self.other, property=make_property())

        self.auth(self.user)
        response = self.api.delete(reverse("remove-favorite", args=[theirs.id]))

        self.assertEqual(response.status_code, 404)
        self.assertTrue(FavoriteProperty.objects.filter(id=theirs.id).exists())

    def test_removing_your_own_saved_home_works(self):
        mine = FavoriteProperty.objects.create(user=self.user, property=make_property())

        self.auth(self.user)
        response = self.api.delete(reverse("remove-favorite", args=[mine.id]))

        self.assertEqual(response.status_code, 204)
        self.assertFalse(FavoriteProperty.objects.filter(id=mine.id).exists())


@override_settings(JWT_SECRET=TEST_SECRET, REST_FRAMEWORK=API_SETTINGS)
class ChangePasswordTests(PortalApiTestCase):
    def url(self):
        return reverse("change-password")

    def test_the_current_password_is_required_to_be_correct(self):
        self.auth(self.user)
        response = self.api.post(self.url(), {
            "current_password": "not the password", "new_password": "a different long passphrase",
        }, format="json")

        self.assertEqual(response.status_code, 400)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(PASSWORD))

    def test_a_weak_new_password_is_refused_by_the_project_validators(self):
        self.auth(self.user)
        response = self.api.post(self.url(), {
            "current_password": PASSWORD, "new_password": "short",
        }, format="json")

        self.assertEqual(response.status_code, 400)
        self.assertIn("new_password", response.json())

    def test_reusing_the_same_password_is_refused(self):
        self.auth(self.user)
        response = self.api.post(self.url(), {
            "current_password": PASSWORD, "new_password": PASSWORD,
        }, format="json")

        self.assertEqual(response.status_code, 400)

    def test_a_successful_change_revokes_every_live_refresh_token(self):
        """
        The reason to change a password is usually that someone else has it.
        Leaving their refresh token live would make the change pointless.
        """
        import uuid

        RefreshToken.objects.create(
            user=self.user, family_id=uuid.uuid4(), token_hash="a" * 64,
            expires_at=timezone.now() + timezone.timedelta(days=7),
        )

        self.auth(self.user)
        response = self.api.post(self.url(), {
            "current_password": PASSWORD, "new_password": "an entirely different passphrase",
        }, format="json")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["sessions_revoked"], 1)
        self.assertFalse(
            RefreshToken.objects.filter(user=self.user, revoked_at__isnull=True).exists(),
        )
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("an entirely different passphrase"))

@override_settings(JWT_SECRET=TEST_SECRET, REST_FRAMEWORK=API_SETTINGS)
class FavouriteToggleTests(PortalApiTestCase):
    def setUp(self):
        super().setUp()
        self.home = make_property(is_published=True, status="available")

    def test_toggling_saves_then_unsaves(self):
        self.auth(self.user)
        url = reverse("toggle-favorite")

        first = self.api.post(url, {"property": str(self.home.id)}, format="json")
        self.assertEqual(first.status_code, 201)
        self.assertTrue(first.json()["saved"])
        self.assertEqual(FavoriteProperty.objects.filter(user=self.user).count(), 1)

        second = self.api.post(url, {"property": str(self.home.id)}, format="json")
        self.assertEqual(second.status_code, 200)
        self.assertFalse(second.json()["saved"])
        self.assertEqual(FavoriteProperty.objects.filter(user=self.user).count(), 0)

    def test_saving_twice_cannot_duplicate_a_row(self):
        """The unique constraint is the guard; this proves the view respects it."""
        FavoriteProperty.objects.create(user=self.user, property=self.home)
        self.auth(self.user)
        self.api.post(reverse("toggle-favorite"), {"property": str(self.home.id)}, format="json")
        self.api.post(reverse("toggle-favorite"), {"property": str(self.home.id)}, format="json")
        self.assertEqual(FavoriteProperty.objects.filter(user=self.user).count(), 1)

    def test_an_unpublished_home_cannot_be_saved(self):
        hidden = make_property(is_published=False)
        self.auth(self.user)
        response = self.api.post(
            reverse("toggle-favorite"), {"property": str(hidden.id)}, format="json",
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(FavoriteProperty.objects.count(), 0)

    def test_anonymous_callers_cannot_toggle(self):
        self.assertEqual(
            self.api.post(reverse("toggle-favorite"), {"property": str(self.home.id)}, format="json").status_code,
            401,
        )

    def test_merge_folds_a_guest_list_into_the_account(self):
        other = make_property(is_published=True, status="available")
        self.auth(self.user)
        response = self.api.post(
            reverse("merge-favorites"),
            {"properties": [str(self.home.id), str(other.id)]}, format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["added"], 2)

    def test_merge_is_additive_and_never_removes(self):
        FavoriteProperty.objects.create(user=self.user, property=self.home)
        other = make_property(is_published=True, status="available")
        self.auth(self.user)
        self.api.post(reverse("merge-favorites"), {"properties": [str(other.id)]}, format="json")
        self.assertEqual(FavoriteProperty.objects.filter(user=self.user).count(), 2)

    def test_merge_skips_ids_that_no_longer_resolve(self):
        import uuid as _uuid

        self.auth(self.user)
        response = self.api.post(
            reverse("merge-favorites"),
            {"properties": [str(self.home.id), str(_uuid.uuid4())]}, format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["added"], 1)

