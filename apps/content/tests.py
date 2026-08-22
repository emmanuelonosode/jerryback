"""
Hiring API tests.

Candidate records are an outsider's personal data, so these are mostly about
who can read them and what a reviewer is allowed to change.
"""

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import Role, User

from .models import JobApplication, JobApplicationStatus

TEST_SECRET = "a-test-jwt-secret-that-is-long-enough-32"
API_SETTINGS = {
    "DEFAULT_AUTHENTICATION_CLASSES": ["apps.accounts.authentication.JWTAuthentication"],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAuthenticated"],
}


@override_settings(JWT_SECRET=TEST_SECRET, REST_FRAMEWORK=API_SETTINGS)
class HiringApiTests(TestCase):
    def setUp(self):
        self.api = APIClient()
        self.candidate = JobApplication.objects.create(
            role_title="Leasing Consultant", full_name="Ada Lovelace",
            email="ada@example.com", motivation="I like helping people find homes.",
        )

    def user(self, role, email=None):
        return User.objects.create_user(
            email=email or f"{role.lower()}@example.com",
            password="correct horse battery staple",
            first_name=role.title(), last_name="User", role=role,
        )

    def auth(self, user):
        from apps.accounts import jwt as jwt_codec

        token = jwt_codec.encode(subject=str(user.pk), role=user.role, token_type="access")
        self.api.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")

    # ---- who can see candidates -----------------------------------------

    def test_anonymous_callers_cannot_list_candidates(self):
        self.assertEqual(self.api.get(reverse("job-applications")).status_code, 401)

    def test_a_resident_cannot_list_candidates(self):
        self.auth(self.user(Role.CLIENT))
        self.assertEqual(self.api.get(reverse("job-applications")).status_code, 403)

    def test_an_agent_cannot_list_candidates(self):
        """AGENT holds no hiring grant — roles are not a hierarchy here."""
        self.auth(self.user(Role.AGENT))
        self.assertEqual(self.api.get(reverse("job-applications")).status_code, 403)

    def test_a_manager_can_list_candidates(self):
        self.auth(self.user(Role.MANAGER))
        response = self.api.get(reverse("job-applications"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()), 1)

    def test_an_admin_can_list_candidates(self):
        self.auth(self.user(Role.ADMIN))
        self.assertEqual(self.api.get(reverse("job-applications")).status_code, 200)

    # ---- reviewing -------------------------------------------------------

    def test_a_resident_cannot_change_a_candidates_status(self):
        self.auth(self.user(Role.CLIENT))
        response = self.api.patch(
            reverse("job-application-detail", args=[self.candidate.id]),
            {"status": JobApplicationStatus.HIRED}, format="json",
        )
        self.assertEqual(response.status_code, 403)
        self.candidate.refresh_from_db()
        self.assertEqual(self.candidate.status, JobApplicationStatus.SUBMITTED)

    def test_a_manager_can_record_a_decision_and_notes(self):
        manager = self.user(Role.MANAGER)
        self.auth(manager)
        response = self.api.patch(
            reverse("job-application-detail", args=[self.candidate.id]),
            {"status": JobApplicationStatus.UNDER_REVIEW, "staff_notes": "Strong writing."},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.candidate.refresh_from_db()
        self.assertEqual(self.candidate.status, JobApplicationStatus.UNDER_REVIEW)
        self.assertEqual(self.candidate.staff_notes, "Strong writing.")
        self.assertEqual(self.candidate.reviewed_by_id, manager.id)

    def test_what_the_candidate_wrote_cannot_be_edited_by_staff(self):
        self.auth(self.user(Role.MANAGER))
        self.api.patch(
            reverse("job-application-detail", args=[self.candidate.id]),
            {"full_name": "Someone Else", "motivation": "rewritten"}, format="json",
        )
        self.candidate.refresh_from_db()
        self.assertEqual(self.candidate.full_name, "Ada Lovelace")
        self.assertEqual(self.candidate.motivation, "I like helping people find homes.")

    def test_scheduling_an_interview_without_a_date_is_refused(self):
        self.auth(self.user(Role.MANAGER))
        response = self.api.patch(
            reverse("job-application-detail", args=[self.candidate.id]),
            {"status": JobApplicationStatus.INTERVIEW_SCHEDULED}, format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("interview_date", response.json())

    def test_scheduling_an_interview_with_a_date_is_accepted(self):
        self.auth(self.user(Role.MANAGER))
        when = timezone.now() + timezone.timedelta(days=3)
        response = self.api.patch(
            reverse("job-application-detail", args=[self.candidate.id]),
            {"status": JobApplicationStatus.INTERVIEW_SCHEDULED, "interview_date": when.isoformat()},
            format="json",
        )
        self.assertEqual(response.status_code, 200)

    # ---- filters ---------------------------------------------------------

    def test_search_matches_name_and_email(self):
        JobApplication.objects.create(
            role_title="Maintenance Technician", full_name="Grace Hopper",
            email="grace@example.com",
        )
        self.auth(self.user(Role.MANAGER))
        body = self.api.get(reverse("job-applications"), {"search": "grace"}).json()
        self.assertEqual([c["full_name"] for c in body], ["Grace Hopper"])

    def test_status_filter_narrows_the_list(self):
        JobApplication.objects.create(
            role_title="Property Manager", full_name="Grace Hopper",
            email="grace@example.com", status=JobApplicationStatus.HIRED,
        )
        self.auth(self.user(Role.MANAGER))
        body = self.api.get(reverse("job-applications"), {"status": "HIRED"}).json()
        self.assertEqual([c["full_name"] for c in body], ["Grace Hopper"])
