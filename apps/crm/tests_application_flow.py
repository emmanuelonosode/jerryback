"""
The application record end to end: what a draft save keeps, who can read a
lease, what signing records, what the lease says, and document requests.
"""

import shutil
import tempfile
from datetime import date

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings

EMERGENCY = override_settings(LEASE_EMERGENCY_PHONE="(512) 555-0100")
from django.urls import reverse
from rest_framework.test import APIClient

from apps.accounts.models import Role, User
from apps.core.money import dollars
from apps.properties.models import FeeCadence, FeeCondition, PropertyFee
from apps.properties.tests import make_property

from .lease import build_lease_terms, late_fee_cents, one_year_term
from .models import (
    ApplicationDocument, ApplicationStatus, DocumentRequest, DocumentRequestStatus, Guarantor,
    RentalApplication,
)

SSN = "123-45-6789"


def make_user(email="marcus@example.com", role=Role.CLIENT, **extra):
    return User.objects.create_user(email=email, password="x" * 12, first_name="Marcus", last_name="Vance", role=role, **extra)


def make_application(**over):
    defaults = dict(
        status=ApplicationStatus.APPROVED, first_name="Marcus", last_name="Vance",
        email="marcus@example.com", cell_phone="555-9876", application_fee_cents=dollars(55),
        security_deposit_cents=dollars(1500), lease_admin_fee_cents=0,
        move_in_date=date(2026, 11, 1), landlord_company="Elm Street Holdings LLC",
        landlord_address="100 Congress Ave, Austin, TX 78701",
        # Texas asks the owner about flooding; "None known" is an answer.
        flood_zone_known="no", flood_history="None known",
    )
    if "property" not in over:
        defaults["property"] = make_property(
            address="500 Elm St", city="Austin", state="TX", zip_code="78701",
            bedrooms=3, price_cents=dollars(1500), year_built=1995,
        )
    return RentalApplication.objects.create(**{**defaults, **over})


class DraftSaveTests(TestCase):
    """What a draft PATCH keeps, and where."""

    def setUp(self):
        self.app = RentalApplication.objects.create(status=ApplicationStatus.DRAFT, application_fee_cents=0)
        self.url = f"/api/v1/leads/apply/drafts/{self.app.id}/"

    def patch(self, body):
        return self.client.patch(self.url, body, content_type="application/json")

    def test_the_full_ssn_never_sits_in_the_draft_or_comes_back(self):
        body = self.patch({"idType": "SSN", "ssn": SSN, "driversLicense": "D1234567"}).json()
        self.app.refresh_from_db()
        self.assertNotIn("ssn", self.app.draft_data)
        self.assertNotIn("driversLicense", self.app.draft_data)
        self.assertEqual(self.app.ssn, SSN)  # encrypted column
        self.assertEqual(self.app.ssn_last4, "6789")
        self.assertNotIn(SSN, str(body))
        self.assertEqual(body["ssnLast4"], "6789")
        self.assertTrue(body["hasLicenseOnFile"])

    def test_a_mistyped_ssn_is_not_kept(self):
        body = self.patch({"ssn": "123-45-678"}).json()
        self.assertIsNone(body["ssnLast4"])

    def test_a_blank_ssn_on_resave_keeps_the_one_on_file(self):
        self.patch({"ssn": SSN})
        self.patch({"ssn": "", "firstName": "Marcus"})
        self.app.refresh_from_db()
        self.assertEqual(self.app.ssn_last4, "6789")

    def test_the_forms_real_field_names_reach_the_columns(self):
        self.patch({
            "preferredMoveInDate": "2026-11-01", "idType": "ITIN",
            "hasEviction": False, "hasFelony": True, "hasBankruptcy": False,
            "isActiveMilitary": False, "receivesHousingAssistance": True,
            "householdMonthlyIncomeCents": dollars(5200),
            "hasMinorsOrDependents": True, "dependentCount": 2,
        })
        a = RentalApplication.objects.get(pk=self.app.pk)
        self.assertEqual(a.move_in_date, date(2026, 11, 1))
        self.assertEqual(a.id_type, "ITIN")
        self.assertTrue(a.has_felony_eviction_bankruptcy)
        self.assertTrue(a.has_housing_assistance)
        self.assertFalse(a.is_active_military)
        self.assertEqual(a.gross_monthly_income_cents, dollars(5200))
        self.assertEqual((a.has_kids, a.number_of_kids), (True, 2))

    def test_the_guarantor_is_saved_and_removed(self):
        self.patch({"guarantor": {"fullName": "Robert Vance", "relationship": "Brother", "monthlyIncomeCents": dollars(7000)}})
        self.assertEqual(Guarantor.objects.get(application=self.app).monthly_income_cents, dollars(7000))
        self.patch({"guarantor": None})
        self.assertFalse(Guarantor.objects.filter(application=self.app).exists())


@EMERGENCY
class LeaseAccessTests(TestCase):
    def setUp(self):
        self.api = APIClient()
        self.tenant = make_user()
        self.stranger = make_user("someone@example.com")
        self.app = make_application(user=self.tenant, lease_sent_at="2026-10-01T12:00:00Z")

    def test_anonymous_callers_get_nothing(self):
        self.assertEqual(self.api.get(f"/api/v1/leads/lease/{self.app.id}/").status_code, 401)
        # This used to return the newest applicant's name, email and address to anyone.
        self.assertEqual(self.api.get("/api/v1/leads/lease/latest/").status_code, 401)

    def test_another_person_cannot_read_or_sign_it(self):
        self.api.force_authenticate(self.stranger)
        self.assertEqual(self.api.get(f"/api/v1/leads/lease/{self.app.id}/").status_code, 404)
        self.assertEqual(self.api.get("/api/v1/leads/lease/latest/").status_code, 404)
        res = self.api.post(f"/api/v1/leads/lease/{self.app.id}/sign/", {}, format="json")
        self.assertEqual(res.status_code, 404)

    def test_the_tenant_reads_their_own_lease(self):
        self.api.force_authenticate(self.tenant)
        data = self.api.get("/api/v1/leads/lease/latest/").json()
        self.assertEqual(data["application_id"], str(self.app.id))
        self.assertEqual(data["terms"]["landlord"]["owner"], "Elm Street Holdings LLC")
        self.assertTrue(data["can_sign"])


@EMERGENCY
class LeaseSigningTests(TestCase):
    def setUp(self):
        self.api = APIClient()
        self.tenant = make_user()
        self.api.force_authenticate(self.tenant)
        self.app = make_application(user=self.tenant, lease_sent_at="2026-10-01T12:00:00Z")
        self.url = f"/api/v1/leads/lease/{self.app.id}/sign/"
        self.body = {"signature_url": "data:image/png;base64,AAAA", "signer_name": "Marcus Vance", "consent": True}

    def test_signing_records_who_where_and_exactly_what(self):
        res = self.api.post(self.url, self.body, format="json", HTTP_USER_AGENT="Phone", REMOTE_ADDR="203.0.113.7")
        self.assertEqual(res.status_code, 200)
        self.app.refresh_from_db()
        self.assertEqual(self.app.lease_signer_name, "Marcus Vance")
        self.assertEqual(self.app.lease_signed_ip, "203.0.113.7")
        self.assertEqual(self.app.lease_signed_user_agent, "Phone")
        self.assertEqual(len(self.app.lease_terms_sha256), 64)
        self.assertEqual(self.app.lease_terms_snapshot["landlord"]["owner"], "Elm Street Holdings LLC")
        self.assertTrue(self.app.lease_consent_text)

    def test_it_cannot_be_signed_twice(self):
        self.api.post(self.url, self.body, format="json")
        self.assertEqual(self.api.post(self.url, self.body, format="json").status_code, 409)

    def test_consent_is_required(self):
        res = self.api.post(self.url, {**self.body, "consent": False}, format="json")
        self.assertEqual(res.status_code, 400)

    def test_an_unapproved_or_unsent_lease_cannot_be_signed(self):
        RentalApplication.objects.filter(pk=self.app.pk).update(status=ApplicationStatus.SUBMITTED)
        self.assertEqual(self.api.post(self.url, self.body, format="json").status_code, 409)

    def test_a_lease_missing_the_owner_cannot_be_signed(self):
        RentalApplication.objects.filter(pk=self.app.pk).update(landlord_company="")
        res = self.api.post(self.url, self.body, format="json")
        self.assertEqual(res.status_code, 409)
        self.assertIn("owner", res.json()["detail"])


@EMERGENCY
class LeaseTermsTests(TestCase):
    def test_the_late_fee_is_the_lesser_of_50_dollars_or_5_percent(self):
        self.assertEqual(late_fee_cents(dollars(800)), dollars(40))
        self.assertEqual(late_fee_cents(dollars(2000)), dollars(50))

    def test_the_term_is_one_year_less_a_day(self):
        self.assertEqual(one_year_term(date(2026, 11, 1)), date(2027, 10, 31))
        self.assertEqual(one_year_term(date(2028, 2, 29)), date(2029, 2, 28))

    def test_rent_includes_required_monthly_fees(self):
        app = make_application()
        PropertyFee.objects.create(
            property=app.property, fee_key="trash", label="Trash", amount_cents=dollars(25),
            cadence=FeeCadence.MONTHLY, condition=FeeCondition.REQUIRED,
        )
        terms = build_lease_terms(app)
        self.assertEqual(terms["money"]["total_monthly_rent"], "$1,525")
        self.assertEqual(terms["premises"]["state_name"], "Texas")
        self.assertEqual(terms["premises"]["bedrooms"], "three (3)")

    def test_lead_paint_disclosure_for_old_or_unknown_homes(self):
        self.assertFalse(build_lease_terms(make_application())["premises"]["lead_paint_disclosure"])
        old = make_application(property=make_property(address="1 Old Rd", year_built=1965))
        self.assertTrue(build_lease_terms(old)["premises"]["lead_paint_disclosure"])
        unknown = make_application(property=make_property(address="2 New Rd", year_built=None))
        self.assertTrue(build_lease_terms(unknown)["premises"]["lead_paint_disclosure"])

    def test_nothing_is_invented_when_data_is_missing(self):
        terms = build_lease_terms(make_application(landlord_company="", move_in_date=None, security_deposit_cents=None))
        self.assertFalse(terms["ready"])
        for needed in ("the owner (landlord) of the home", "the move-in date", "the security deposit"):
            self.assertIn(needed, terms["missing"])
        self.assertEqual(terms["term"]["start"], "")

    def text_of(self, terms):
        return " ".join(p["text"] for sec in terms["sections"] for p in sec["paragraphs"])

    def home_in(self, state, **over):
        return make_property(**{"address": f"1 {state} St", "state": state, "price_cents": dollars(1500), "year_built": 2001, **over})

    def test_colorado_waits_seven_days_for_a_late_fee(self):
        terms = build_lease_terms(make_application(property=self.home_in("CO")))
        self.assertEqual(terms["money"]["late_after_days"], 7)
        self.assertIn("seven calendar days", self.text_of(terms))

    def test_entry_notice_is_48_hours_everywhere_and_deposit_back_in_14_days(self):
        for state in ("TX", "FL", "MN", "CA"):
            terms = build_lease_terms(make_application(property=self.home_in(state)))
            self.assertEqual(terms["entry_notice_hours"], 48, state)
            self.assertEqual(terms["money"]["deposit_return_days"], 14, state)

    def test_florida_needs_the_deposit_bank_and_prints_the_statutory_notice(self):
        app = make_application(property=self.home_in("FL"))
        self.assertIn("where the deposit is held (bank name and address)", build_lease_terms(app)["missing"])
        app.deposit_held_at = "First Bank, 1 Main St, Miami FL - non-interest-bearing"
        terms = build_lease_terms(app)
        self.assertTrue(terms["ready"], terms["missing"])
        self.assertIn("YOUR LEASE REQUIRES PAYMENT OF CERTAIN DEPOSITS", self.text_of(terms))
        self.assertIn("RADON GAS", self.text_of(terms))

    def test_a_deposit_over_the_state_cap_blocks_the_lease(self):
        app = make_application(property=self.home_in("CA"), security_deposit_cents=dollars(3000),
                               other_hazards_known="None known")
        self.assertTrue(any("limit" in m for m in build_lease_terms(app)["missing"]))

    def test_texas_prints_the_repair_remedies_in_bold_and_early_termination_rights(self):
        terms = build_lease_terms(make_application())
        bold = [p["text"] for sec in terms["sections"] for p in sec["paragraphs"] if p["strong"]]
        self.assertTrue(any("92.056" in b for b in bold))
        self.assertIn("military deployment or transfer", self.text_of(terms))

    def test_old_homes_carry_the_federal_lead_warning(self):
        app = make_application(property=self.home_in("TX", year_built=1960), lead_hazards_known="None known")
        self.assertIn("Housing built before 1978 may contain lead-based paint", self.text_of(build_lease_terms(app)))

    def test_the_signed_hash_covers_the_words_not_just_the_numbers(self):
        terms = build_lease_terms(make_application())
        self.assertIn("sections", terms)
        self.assertIn("never lock you out", self.text_of(terms))

    def test_assistance_animals_never_carry_a_pet_fee(self):
        app = make_application(pet_fee_cents=dollars(300), animals=[{"animalType": "Dog", "isServiceAnimal": True}])
        self.assertEqual(build_lease_terms(app)["money"]["pet_fee"], "")


class DocumentRequestTests(TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        override = override_settings(PRIVATE_UPLOAD_ROOT=self.tmp)
        override.enable()
        self.addCleanup(override.disable)

        self.api = APIClient()
        self.tenant = make_user()
        self.app = make_application(user=self.tenant, status=ApplicationStatus.SUBMITTED)
        self.req = DocumentRequest.objects.create(application=self.app, kind="INCOME_PROOF", message="Two recent pay stubs")
        self.url = reverse("portal-document-upload", args=[self.req.id])

    def upload(self, content_type="application/pdf", name="stub.pdf"):
        return self.api.post(self.url, {"files": [SimpleUploadedFile(name, b"%PDF-1.4 x", content_type=content_type)]}, format="multipart")

    def test_the_owner_sees_the_request_and_uploads(self):
        self.api.force_authenticate(self.tenant)
        listed = self.api.get(reverse("portal-document-requests")).json()
        self.assertEqual(listed[0]["message"], "Two recent pay stubs")
        res = self.upload()
        self.assertEqual(res.status_code, 201)
        self.req.refresh_from_db()
        self.assertEqual(self.req.status, DocumentRequestStatus.SUBMITTED)
        self.assertEqual(ApplicationDocument.objects.get().original_name, "stub.pdf")

    def test_nobody_else_can_upload_to_it(self):
        self.api.force_authenticate(make_user("someone@example.com"))
        self.assertEqual(self.upload().status_code, 404)
        self.assertEqual(self.api.get(reverse("portal-document-requests")).json(), [])

    def test_a_file_a_browser_would_run_is_refused(self):
        self.api.force_authenticate(self.tenant)
        self.assertEqual(self.upload(content_type="text/html", name="x.html").status_code, 400)
        self.assertFalse(ApplicationDocument.objects.exists())

    def test_a_rejection_must_say_why(self):
        from django.db import IntegrityError, transaction

        with self.assertRaises(IntegrityError), transaction.atomic():
            DocumentRequest.objects.filter(pk=self.req.pk).update(status="REJECTED", review_note="")


class GuarantorPortalTests(TestCase):
    def test_the_tenant_adds_and_edits_a_guarantor_until_the_lease_is_signed(self):
        api = APIClient()
        tenant = make_user()
        api.force_authenticate(tenant)
        app = make_application(user=tenant, status=ApplicationStatus.SUBMITTED)
        url = reverse("portal-guarantor")

        res = api.put(url, {"fullName": "Robert Vance", "monthlyIncomeCents": dollars(6000)}, format="json")
        self.assertEqual(res.json()["guarantor"]["monthlyIncome"], "$6,000")

        RentalApplication.objects.filter(pk=app.pk).update(lease_signed_at="2026-10-02T12:00:00Z")
        self.assertEqual(api.put(url, {"fullName": "Someone Else"}, format="json").status_code, 409)


class AdminPiiTests(TestCase):
    """The full SSN needs the PII grant; an agent sees the last four only."""

    def setUp(self):
        self.app = make_application()
        self.app.ssn, self.app.ssn_last4 = SSN, "6789"
        self.app.save()
        self.url = reverse("admin:crm_rentalapplication_change", args=[self.app.pk])

    def page_for(self, role):
        staff = User.objects.create_user(email=f"{role.lower()}@srg.test", password="x" * 12, role=role, is_staff=True)
        from django.contrib.auth.models import Permission

        staff.user_permissions.add(*Permission.objects.filter(codename__in=["view_rentalapplication", "change_rentalapplication"]))
        self.client.force_login(staff)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def test_an_agent_sees_last_four_and_not_the_full_number(self):
        page = self.page_for(Role.AGENT)
        self.assertIn("6789", page)
        self.assertNotIn(SSN, page)

    def test_an_admin_with_the_pii_grant_sees_it(self):
        self.assertIn(SSN, self.page_for(Role.ADMIN))
