"""
Portal API tests.

The bulk of these are authorisation tests. A maintenance ticket says when a
resident is not home and a document set contains their identity papers, so the
failure that matters here is not a broken field — it is one resident reading
another's row.
"""

from datetime import timedelta

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import Role, User
from apps.crm.models import Client, Lead

from .models import (
    ClientDocument,
    DocumentType,
    MaintenanceCategory,
    MaintenancePriority,
    MaintenanceRequest,
    MaintenanceStatus,
)

TEST_SECRET = "a-test-jwt-secret-that-is-long-enough-32"

API_SETTINGS = {
    "DEFAULT_AUTHENTICATION_CLASSES": ["apps.accounts.authentication.JWTAuthentication"],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAuthenticated"],
}


def make_resident(email, first="Ada", last="Lovelace"):
    user = User.objects.create_user(
        email=email, password="correct horse battery staple",
        first_name=first, last_name=last, role=Role.CLIENT,
    )
    lead = Lead.objects.create(full_name=f"{first} {last}", email=email)
    return user, Client.objects.create(user=user, lead=lead)


@override_settings(JWT_SECRET=TEST_SECRET, REST_FRAMEWORK=API_SETTINGS)
class MaintenanceApiTests(TestCase):
    def setUp(self):
        self.client_api = APIClient()
        self.user, self.resident = make_resident("ada@example.com")
        self.other_user, self.other_resident = make_resident("grace@example.com", "Grace", "Hopper")
        self.url = reverse("portal-maintenance")

    def auth(self, user):
        from apps.accounts import jwt as jwt_codec

        token = jwt_codec.encode(subject=str(user.pk), role=user.role, token_type="access")
        self.client_api.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")

    def make_ticket(self, resident, **over):
        defaults = dict(
            client=resident, title="Leaking kitchen tap",
            description="Water pools under the sink overnight.",
            category=MaintenanceCategory.PLUMBING, priority=MaintenancePriority.MEDIUM,
        )
        return MaintenanceRequest.objects.create(**{**defaults, **over})

    # ---- authorisation ---------------------------------------------------

    def test_anonymous_callers_are_rejected(self):
        self.assertEqual(self.client_api.get(self.url).status_code, 401)

    def test_a_resident_never_sees_another_residents_tickets(self):
        self.make_ticket(self.other_resident, title="Grace's broken heater")
        mine = self.make_ticket(self.resident)

        self.auth(self.user)
        body = self.client_api.get(self.url).json()

        self.assertEqual([t["id"] for t in body], [str(mine.id)])

    def test_a_client_id_in_the_payload_cannot_reassign_the_ticket(self):
        """The classic IDOR: post someone else's owner and see if it sticks."""
        self.auth(self.user)
        response = self.client_api.post(self.url, {
            "title": "Front door will not lock",
            "description": "The deadbolt does not throw at all.",
            "category": MaintenanceCategory.SECURITY,
            "client": str(self.other_resident.id),
        }, format="json")

        self.assertEqual(response.status_code, 201)
        ticket = MaintenanceRequest.objects.get(id=response.json()["id"])
        self.assertEqual(ticket.client_id, self.resident.id)

    def test_a_resident_cannot_resolve_their_own_ticket_on_create(self):
        self.auth(self.user)
        response = self.client_api.post(self.url, {
            "title": "Bathroom extractor fan dead",
            "description": "No noise and no airflow when switched on.",
            "category": MaintenanceCategory.ELECTRICAL,
            "status": MaintenanceStatus.RESOLVED,
            "staff_notes": "Marked done by the tenant.",
        }, format="json")

        self.assertEqual(response.status_code, 201)
        ticket = MaintenanceRequest.objects.get(id=response.json()["id"])
        self.assertEqual(ticket.status, MaintenanceStatus.SUBMITTED)
        self.assertEqual(ticket.staff_notes, "")

    def test_a_registered_user_who_is_not_yet_a_client_gets_an_empty_list(self):
        stranger = User.objects.create_user(
            email="new@example.com", password="correct horse battery staple",
            first_name="New", last_name="Person", role=Role.CLIENT,
        )
        self.auth(stranger)
        response = self.client_api.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [])

    # ---- behaviour -------------------------------------------------------

    def test_urgency_ordering_puts_high_above_low(self):
        """
        The model's `-priority` sorts the string, which ranks HIGH below LOW.
        This is the regression test for that.
        """
        self.make_ticket(self.resident, title="Low", priority=MaintenancePriority.LOW)
        self.make_ticket(self.resident, title="High", priority=MaintenancePriority.HIGH)
        self.make_ticket(self.resident, title="Urgent", priority=MaintenancePriority.URGENT)

        self.auth(self.user)
        titles = [t["title"] for t in self.client_api.get(self.url).json()]

        self.assertEqual(titles, ["Urgent", "High", "Low"])

    def test_state_filter_separates_active_from_resolved(self):
        self.make_ticket(self.resident, title="Open one")
        self.make_ticket(self.resident, title="Done one", status=MaintenanceStatus.RESOLVED)

        self.auth(self.user)
        active = self.client_api.get(self.url, {"state": "active"}).json()
        resolved = self.client_api.get(self.url, {"state": "resolved"}).json()

        self.assertEqual([t["title"] for t in active], ["Open one"])
        self.assertEqual([t["title"] for t in resolved], ["Done one"])

    def test_a_thin_description_is_refused_with_a_usable_message(self):
        self.auth(self.user)
        response = self.client_api.post(self.url, {
            "title": "Broken", "description": "bad", "category": MaintenanceCategory.OTHER,
        }, format="json")

        self.assertEqual(response.status_code, 400)
        self.assertIn("description", response.json())

    def test_priority_defaults_to_medium_when_not_supplied(self):
        self.auth(self.user)
        response = self.client_api.post(self.url, {
            "title": "Dishwasher not draining",
            "description": "Standing water in the base after every cycle.",
            "category": MaintenanceCategory.APPLIANCE,
        }, format="json")

        self.assertEqual(response.json()["priority"], MaintenancePriority.MEDIUM)


@override_settings(JWT_SECRET=TEST_SECRET, REST_FRAMEWORK=API_SETTINGS)
class DocumentApiTests(TestCase):
    def setUp(self):
        self.client_api = APIClient()
        self.user, self.resident = make_resident("ada@example.com")
        self.other_user, self.other_resident = make_resident("grace@example.com", "Grace", "Hopper")
        self.url = reverse("portal-documents")

    def auth(self, user):
        from apps.accounts import jwt as jwt_codec

        token = jwt_codec.encode(subject=str(user.pk), role=user.role, token_type="access")
        self.client_api.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")

    def make_doc(self, resident, name, **over):
        return ClientDocument.objects.create(
            client=resident, name=name, file_url=f"/media/{name}",
            **{"document_type": DocumentType.CONTRACT, **over},
        )

    def test_identity_documents_do_not_leak_between_residents(self):
        self.make_doc(self.other_resident, "grace-passport.pdf", document_type=DocumentType.ID_DOCUMENT)
        mine = self.make_doc(self.resident, "my-lease.pdf")

        self.auth(self.user)
        body = self.client_api.get(self.url).json()

        self.assertEqual([d["id"] for d in body], [str(mine.id)])

    def test_expiry_inside_thirty_days_is_flagged(self):
        self.make_doc(self.resident, "expiring.pdf", expires_at=timezone.now() + timedelta(days=10))
        self.auth(self.user)
        self.assertTrue(self.client_api.get(self.url).json()[0]["expires_soon"])

    def test_expiry_beyond_thirty_days_is_not_flagged(self):
        self.make_doc(self.resident, "fine.pdf", expires_at=timezone.now() + timedelta(days=90))
        self.auth(self.user)
        self.assertFalse(self.client_api.get(self.url).json()[0]["expires_soon"])

    def test_a_document_with_no_expiry_is_not_flagged(self):
        self.make_doc(self.resident, "forever.pdf")
        self.auth(self.user)
        self.assertFalse(self.client_api.get(self.url).json()[0]["expires_soon"])

    def test_type_filter_narrows_the_list(self):
        self.make_doc(self.resident, "lease.pdf", document_type=DocumentType.CONTRACT)
        self.make_doc(self.resident, "receipt.pdf", document_type=DocumentType.RECEIPT)

        self.auth(self.user)
        body = self.client_api.get(self.url, {"type": "receipt"}).json()

        self.assertEqual([d["name"] for d in body], ["receipt.pdf"])
