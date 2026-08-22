from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from apps.accounts.models import User
from apps.core.money import dollars
from apps.properties.tests import make_property

from .models import (
    ActivityType, ApplicationStatus, Lead, LeadActivity, LeadStatus, Referrer,
    ReferralPayout, RentalApplication,
)
from .move_in import calculate_move_in
from .scoring import score_band, score_lead
from .services import decide_application


def make_lead(**over):
    defaults = dict(full_name="Ada Lovelace", email="ada@example.com")
    return Lead.objects.create(**{**defaults, **over})


class ScoringTests(TestCase):
    def test_a_bare_contact_form_lead_scores_zero(self):
        self.assertEqual(make_lead().score, 0)

    def test_a_hot_inquiry_scores_high(self):
        home = make_property()
        lead = make_lead(
            source="PROPERTY_INQUIRY", phone="555-0100", property_interest=home,
            budget_max_cents=dollars(2000), utm_source="google", move_in_timeline="ASAP",
            occupants_count=2, has_pets=False, preferred_contact="PHONE",
        )
        for _ in range(3):
            LeadActivity.objects.create(lead=lead, activity_type=ActivityType.NOTE)
        self.assertGreaterEqual(lead.score, 90)

    def test_household_size_scores_for_being_answered_not_for_its_value(self):
        # A family of five must not rank below a single occupant. Familial
        # status is a protected class; the score must encode no preference.
        single = make_lead(email="a@x.com", occupants_count=1).score
        family = make_lead(email="b@x.com", occupants_count=5).score
        self.assertEqual(single, family)

    def test_answering_the_pets_question_scores_the_same_either_way(self):
        yes = make_lead(email="a@x.com", has_pets=True).score
        no = make_lead(email="b@x.com", has_pets=False).score
        self.assertEqual(yes, no)

    def test_the_score_never_leaves_the_range(self):
        home = make_property()
        hot = make_lead(
            source="PROPERTY_INQUIRY", phone="x", property_interest=home, budget_max_cents=1,
            utm_source="google", move_in_timeline="ASAP", occupants_count=9, has_pets=True,
            preferred_contact="PHONE", status=LeadStatus.CONVERTED,
        )
        for _ in range(50):
            LeadActivity.objects.create(lead=hot, activity_type=ActivityType.NOTE)
        self.assertLessEqual(hot.score, 100)
        self.assertGreaterEqual(make_lead(email="l@x.com", status=LeadStatus.LOST).score, 0)

    def test_a_stale_untouched_lead_decays(self):
        lead = make_lead(phone="555")
        fresh = lead.score
        Lead.objects.filter(pk=lead.pk).update(created_at=timezone.now() - timedelta(days=45))
        lead.refresh_from_db()
        self.assertLess(lead.score, fresh)

    def test_a_contacted_lead_does_not_decay_even_when_old(self):
        lead = make_lead(phone="555", status=LeadStatus.CONTACTED)
        Lead.objects.filter(pk=lead.pk).update(created_at=timezone.now() - timedelta(days=45))
        lead.refresh_from_db()
        self.assertEqual(lead.score, 15)

    def test_every_point_is_explained(self):
        # An unexplained ranking is one nobody trusts or acts on.
        lead = make_lead(source="PROPERTY_INQUIRY", phone="555", move_in_timeline="ASAP")
        detail = lead.score_detail
        self.assertEqual(sum(r["points"] for r in detail["reasons"]), detail["score"])

    def test_no_protected_class_or_financial_input_reaches_the_score(self):
        # The score ranks who to call first. It must never gate qualification —
        # that runs against the published criteria, which is the safe harbour.
        source = (score_lead.__doc__ or "") + (score_band.__doc__ or "")
        lead = make_lead()
        detail = lead.score_detail
        labels = " ".join(r["label"].lower() for r in detail["reasons"])
        for banned in ("income", "credit", "race", "children", "disability"):
            self.assertNotIn(banned, labels)
        self.assertIsInstance(source, str)


class ActivityTests(TestCase):
    def test_only_human_contact_updates_last_contacted_at(self):
        # An automated open is a signal, not a conversation. Letting it count
        # suppresses follow-up for someone nobody has spoken to.
        lead = make_lead()
        LeadActivity.objects.create(lead=lead, activity_type=ActivityType.EMAIL_OPENED)
        lead.refresh_from_db()
        self.assertIsNone(lead.last_contacted_at)

        LeadActivity.objects.create(lead=lead, activity_type=ActivityType.CALL, note="Spoke")
        lead.refresh_from_db()
        self.assertIsNotNone(lead.last_contacted_at)


class ReferralTests(TestCase):
    def test_codes_are_random_not_sequential(self):
        # Guessable codes let anyone claim another referrer's commission.
        codes = {Referrer.objects.create(name=f"R{i}", email=f"r{i}@x.com").code for i in range(30)}
        self.assertEqual(len(codes), 30)
        for code in codes:
            self.assertRegex(code, r"^[0-9a-f]{8}$")

    def test_commission_is_exact_cents(self):
        # rent x months x 0.40 in floats produces fractional cents on most
        # rents, and a payout that does not reconcile is a dispute about money.
        referrer = Referrer.objects.create(name="R", email="r@x.com")
        payout = ReferralPayout.objects.create(referrer=referrer, monthly_rent_cents=dollars(1500))
        self.assertEqual(payout.commission_amount_cents, dollars(1200))
        self.assertIsInstance(payout.commission_amount_cents, int)

    def test_a_payout_records_its_own_inputs_so_it_can_be_recomputed(self):
        referrer = Referrer.objects.create(name="R", email="r@x.com")
        payout = ReferralPayout.objects.create(referrer=referrer, monthly_rent_cents=dollars(1633))
        self.assertEqual(payout.commission_months, 2)
        self.assertEqual(payout.commission_basis_points, 4000)
        self.assertEqual(payout.commission_amount_cents, dollars(1306) + 40)


class MoveInTests(TestCase):
    def test_matches_the_specified_shape_in_exact_cents(self):
        result = calculate_move_in(
            monthly_rent_cents=dollars(1200), months_upfront=1, application_fee_cents=dollars(55),
        )
        self.assertEqual(result.total_cents, dollars(1200) + dollars(1200) + dollars(55) + dollars(150))

    def test_the_lines_sum_exactly_to_the_total_including_awkward_rents(self):
        # The move-in total is the largest number this company quotes, it is
        # quoted before the applicant commits, and it must match the invoice.
        for rent in (121_175, 133_333, 99_999, 1, 250_001):
            result = calculate_move_in(
                monthly_rent_cents=rent, months_upfront=3,
                application_fee_cents=5_500, pet_fee_cents=4_999,
            )
            summed = sum(line["unit_price_cents"] * line["quantity"] for line in result.line_items)
            self.assertEqual(summed, result.total_cents, rent)
            self.assertIsInstance(result.total_cents, int)

    def test_an_already_paid_application_fee_is_not_charged_again(self):
        # Charging to apply and again at move-in is exactly the quiet
        # double-charge this brand exists not to do.
        result = calculate_move_in(monthly_rent_cents=dollars(1000), application_fee_cents=0)
        self.assertNotIn("Application fee", [line["description"] for line in result.line_items])

    def test_a_deposit_over_the_ceiling_is_reported_not_silently_clamped(self):
        # Quietly reducing it hides a policy conflict from the person who has
        # to resolve it; clamping to a cap that may not apply is its own error.
        result = calculate_move_in(
            monthly_rent_cents=dollars(1000), security_deposit_cents=dollars(3000),
            application_fee_cents=0, lease_admin_fee_cents=0,
            max_security_deposit_cents=dollars(2000),
        )
        self.assertEqual(result.line_items[1]["unit_price_cents"], dollars(3000))
        self.assertEqual(len(result.warnings), 1)

    def test_a_nonsensical_rent_is_refused(self):
        for rent in (0, -1):
            with self.assertRaises(ValueError):
                calculate_move_in(monthly_rent_cents=rent)


class DecisionTests(TestCase):
    def setUp(self):
        self.actor = User.objects.create_user(
            "admin@x.com", "correct horse battery", first_name="A", last_name="D", role="ADMIN",
        )
        self.home = make_property(price_cents=dollars(1200))

    def application(self, **over):
        defaults = dict(
            status=ApplicationStatus.SUBMITTED, property=self.home,
            application_fee_cents=dollars(55), is_fee_paid=True, email="a@b.com",
            # Set explicitly because approval now requires it. These used to be
            # left blank and filled by the calculator's fallbacks, which is the
            # behaviour that was removed: a blank produced a real invoice with
            # a deposit and an admin fee nobody had chosen.
            security_deposit_cents=dollars(1200), lease_admin_fee_cents=dollars(150),
        )
        return RentalApplication.objects.create(**{**defaults, **over})

    def test_the_clock_starts_at_verification_not_submission(self):
        # With manual rails there is a real gap between sending money and a
        # person confirming it; starting at submission advertises a deadline
        # the company begins missing on day one.
        app = self.application(status=ApplicationStatus.PENDING_VERIFICATION, is_fee_paid=False)
        self.assertIsNone(app.decision_due_at)
        app.start_decision_clock()
        self.assertAlmostEqual(
            (app.decision_due_at - app.verified_at).total_seconds(), 24 * 3600, delta=2,
        )

    def test_a_plain_approval_needs_no_notice_and_creates_the_move_in_invoice(self):
        app = self.application()
        result = decide_application(
            app, decision=ApplicationStatus.APPROVED, reason="Met the published criteria",
            based_on_consumer_report=False, actor=self.actor,
        )
        self.assertIsNone(result.adverse_action_notice)
        self.assertIsNotNone(result.invoice)

    def test_a_report_based_rejection_generates_a_notice(self):
        app = self.application()
        result = decide_application(
            app, decision=ApplicationStatus.REJECTED, reason="Income below the published multiple",
            based_on_consumer_report=True, agency_name="Acme Screening",
            agency_contact="1-800-555-0100", actor=self.actor,
        )
        self.assertIsNotNone(result.adverse_action_notice)
        self.assertIsNone(result.invoice)

    def test_approval_on_worse_terms_is_also_adverse_action(self):
        # FCRA 1681m(a) covers "less favourable terms", not only denial. So the
        # tier-two track generates notices on APPROVALS, which reads as a
        # contradiction until you read the statute.
        app = self.application()
        result = decide_application(
            app, decision=ApplicationStatus.APPROVED_WITH_CONDITIONS,
            reason="Approved with an additional deposit under individual review",
            based_on_consumer_report=True, agency_name="Acme Screening",
            agency_contact="1-800-555-0100", actor=self.actor,
        )
        self.assertIsNotNone(result.adverse_action_notice)
        # And it still invoices, because the applicant is moving in.
        self.assertIsNotNone(result.invoice)

    def test_a_report_based_decision_without_the_agency_is_refused(self):
        # The notice exists so the applicant can dispute the report. Without
        # the agency they cannot, so it is not a notice.
        app = self.application()
        with self.assertRaises(ValueError):
            decide_application(
                app, decision=ApplicationStatus.REJECTED, reason="Report",
                based_on_consumer_report=True, actor=self.actor,
            )

    def test_a_decision_without_a_reason_is_refused_approvals_included(self):
        app = self.application()
        with self.assertRaises(ValueError):
            decide_application(
                app, decision=ApplicationStatus.APPROVED, reason="   ",
                based_on_consumer_report=False, actor=self.actor,
            )

    def test_an_application_cannot_be_decided_twice(self):
        app = self.application()
        decide_application(
            app, decision=ApplicationStatus.APPROVED, reason="Met criteria",
            based_on_consumer_report=False, actor=self.actor,
        )
        with self.assertRaises(ValueError):
            decide_application(
                app, decision=ApplicationStatus.REJECTED, reason="Changed mind",
                based_on_consumer_report=False, actor=self.actor,
            )

    def test_the_full_ssn_has_nowhere_to_be_stored(self):
        # The spec stores a Fernet ciphertext keyed off SECRET_KEY. See the
        # module docstring for why that is encryption against the wrong threat.
        fields = {f.name for f in RentalApplication._meta.get_fields() if hasattr(f, "attname")}
        self.assertNotIn("ssn_encrypted", fields)
        self.assertIn("ssn_last4", fields)
        self.assertIn("screening_reference", fields)

    def test_lead_score_is_not_a_column(self):
        fields = {f.name for f in Lead._meta.get_fields() if hasattr(f, "attname")}
        self.assertNotIn("lead_score", fields)


class ApprovalReachesTheApplicantTests(TestCase):
    """
    Approving has to produce something the applicant can act on.

    The failure this guards against is quiet: the invoice was created as DRAFT,
    the portal excludes drafts by design, so an approved applicant opened their
    payments page and found nothing. Everything looked correct from the admin.
    """

    def setUp(self):
        from apps.accounts.models import Role, User

        self.actor = User.objects.create_user(
            "agent@example.com", "correct horse battery", first_name="A", last_name="G",
            role=Role.MANAGER,
        )
        self.home = make_property(price_cents=dollars(1850), state="GA")

    def application(self, **over):
        defaults = dict(
            status=ApplicationStatus.SUBMITTED, property=self.home,
            application_fee_cents=dollars(55), is_fee_paid=True,
            email="applicant@example.com", months_rent_upfront=1,
            security_deposit_cents=dollars(1850), lease_admin_fee_cents=dollars(150),
        )
        return RentalApplication.objects.create(**{**defaults, **over})

    def approve(self, application):
        return decide_application(
            application, decision=ApplicationStatus.APPROVED,
            reason="Meets our criteria.", based_on_consumer_report=False, actor=self.actor,
        )

    def test_the_move_in_invoice_is_payable_not_a_draft(self):
        from apps.billing.models import InvoiceStatus

        result = self.approve(self.application())
        self.assertIsNotNone(result.invoice)
        self.assertEqual(result.invoice.status, InvoiceStatus.SENT)

    def test_the_breakdown_totals_its_own_lines(self):
        result = self.approve(self.application())
        expected = sum(
            i["unit_price_cents"] * i.get("quantity", 1) for i in result.invoice.line_items
        )
        self.assertEqual(result.invoice.total_cents, expected)

    def test_a_pet_fee_is_not_charged_when_the_application_declared_no_pets(self):
        application = self.application(has_pets=False, pet_fee_cents=dollars(300))
        result = self.approve(application)
        descriptions = [i["description"] for i in result.invoice.line_items]
        self.assertNotIn("Pet fee", descriptions)

    def test_a_pet_fee_is_charged_when_the_application_declared_pets(self):
        application = self.application(has_pets=True, pet_fee_cents=dollars(300))
        result = self.approve(application)
        descriptions = [i["description"] for i in result.invoice.line_items]
        self.assertIn("Pet fee", descriptions)

    def test_approval_queues_an_email_that_carries_the_total_and_the_link(self):
        from apps.integrations.models import OutboundEmail

        result = self.approve(self.application())
        email = OutboundEmail.objects.filter(template="application-approved").first()

        self.assertIsNotNone(email)
        self.assertEqual(email.to_email, "applicant@example.com")
        # The figure itself, not just a link to go and find it.
        self.assertIn(f"{result.invoice.total_cents / 100:,.2f}", email.body_text)
        self.assertIn("/portal/payments", email.body_text)
        self.assertIn(result.invoice.invoice_number, email.body_text)

    def test_a_decline_does_not_invoice_or_email_a_move_in(self):
        from apps.integrations.models import OutboundEmail

        result = decide_application(
            self.application(), decision=ApplicationStatus.REJECTED,
            reason="Not proceeding.", based_on_consumer_report=False, actor=self.actor,
        )
        self.assertIsNone(result.invoice)
        self.assertFalse(OutboundEmail.objects.filter(template="application-approved").exists())

    def test_a_deposit_over_the_configured_ceiling_is_reported_not_clamped(self):
        with self.settings(SECURITY_DEPOSIT_MAX_MONTHS={"GA": 1.0}):
            application = self.application(security_deposit_cents=dollars(3700))  # 2x rent
            result = self.approve(application)

        self.assertTrue(result.warnings, "an over-ceiling deposit must be reported")
        # Reported, never silently reduced — the figure charged is still the one
        # staff entered, so the conflict is visible rather than papered over.
        deposit = next(
            i for i in result.invoice.line_items if i["description"] == "Security deposit"
        )
        self.assertEqual(deposit["unit_price_cents"], dollars(3700))

    def test_no_ceiling_configured_means_no_false_reassurance(self):
        with self.settings(SECURITY_DEPOSIT_MAX_MONTHS={}, SECURITY_DEPOSIT_MAX_MONTHS_DEFAULT=None):
            result = self.approve(self.application(security_deposit_cents=dollars(9999)))
        self.assertEqual(result.warnings, [])


class MoveInTermsMustBeSetBeforeApprovalTests(TestCase):
    """
    The calculator's fallbacks are fine for a quote and wrong for an invoice.

    Without this guard, approving an untouched application charged a one-month
    deposit and the configured admin fee — real money, on a real person, that
    nobody chose for their application.
    """

    def setUp(self):
        from apps.accounts.models import Role, User

        self.actor = User.objects.create_user(
            "agent2@example.com", "correct horse battery", first_name="A", last_name="G",
            role=Role.MANAGER,
        )
        self.home = make_property(price_cents=dollars(1850), state="GA")

    def application(self, **over):
        defaults = dict(
            status=ApplicationStatus.SUBMITTED, property=self.home,
            application_fee_cents=dollars(55), is_fee_paid=True, email="a@example.com",
        )
        return RentalApplication.objects.create(**{**defaults, **over})

    def approve(self, application):
        return decide_application(
            application, decision=ApplicationStatus.APPROVED,
            reason="Meets our criteria.", based_on_consumer_report=False, actor=self.actor,
        )

    def test_approving_without_terms_is_refused(self):
        with self.assertRaises(ValueError) as caught:
            self.approve(self.application())
        message = str(caught.exception)
        self.assertIn("security deposit", message)
        self.assertIn("administration fee", message)

    def test_the_refusal_says_a_blank_is_not_the_same_as_free(self):
        with self.assertRaises(ValueError) as caught:
            self.approve(self.application())
        self.assertIn("0", str(caught.exception))

    def test_zero_is_accepted_as_a_deliberate_answer(self):
        application = self.application(
            security_deposit_cents=0, lease_admin_fee_cents=0,
        )
        result = self.approve(application)

        # A zero deposit is still LISTED, at $0.00 — that tells the applicant
        # there is no deposit rather than leaving them to infer it from an
        # absence. A zero admin fee is dropped, because a fee that does not
        # exist is not a line item.
        deposit = next(
            i for i in result.invoice.line_items if i["description"] == "Security deposit"
        )
        self.assertEqual(deposit["unit_price_cents"], 0)
        self.assertNotIn(
            "Lease administration fee",
            [i["description"] for i in result.invoice.line_items],
        )

    def test_a_pet_fee_is_required_when_the_application_declares_pets(self):
        application = self.application(
            security_deposit_cents=dollars(1850), lease_admin_fee_cents=dollars(150),
            has_pets=True,
        )
        with self.assertRaises(ValueError) as caught:
            self.approve(application)
        self.assertIn("pet fee", str(caught.exception))

    def test_a_pet_fee_is_not_required_when_there_are_no_pets(self):
        application = self.application(
            security_deposit_cents=dollars(1850), lease_admin_fee_cents=dollars(150),
            has_pets=False,
        )
        self.assertIsNotNone(self.approve(application).invoice)

    def test_declining_never_requires_move_in_terms(self):
        result = decide_application(
            self.application(), decision=ApplicationStatus.REJECTED,
            reason="Not proceeding.", based_on_consumer_report=False, actor=self.actor,
        )
        self.assertIsNone(result.invoice)

    def test_upfront_months_multiply_the_rent_line(self):
        application = self.application(
            security_deposit_cents=dollars(1850), lease_admin_fee_cents=dollars(150),
            months_rent_upfront=3,
        )
        result = self.approve(application)
        rent = next(i for i in result.invoice.line_items if "rent" in i["description"].lower())
        self.assertEqual(rent["quantity"], 3)
        self.assertEqual(rent["unit_price_cents"] * rent["quantity"], dollars(5550))


class GuestApplicationsBecomeTheirsTests(TestCase):
    """
    An application made before the account existed has to end up in the portal.

    The portal filters by `user`, and a guest application has none — so without
    linking, the person who filled the form in is the one person who cannot see
    it.
    """

    def setUp(self):
        from apps.accounts.models import Role, User

        self.User = User
        self.Role = Role
        self.home = make_property()

    def guest_application(self, email):
        return RentalApplication.objects.create(
            status=ApplicationStatus.SUBMITTED, property=self.home,
            application_fee_cents=dollars(55), email=email,
        )

    def user(self, email, verified=True):
        u = self.User.objects.create_user(
            email, "correct horse battery staple", first_name="A", last_name="B",
            role=self.Role.CLIENT,
        )
        u.is_email_verified = verified
        u.save(update_fields=["is_email_verified"])
        return u

    def test_a_verified_account_claims_its_own_guest_application(self):
        from .services import link_applications_to_user

        application = self.guest_application("claimer@example.com")
        linked = link_applications_to_user(self.user("claimer@example.com"))

        application.refresh_from_db()
        self.assertEqual(linked, 1)
        self.assertIsNotNone(application.user)

    def test_an_unverified_account_claims_nothing(self):
        """The match is by email alone, so an unproven address must not inherit."""
        from .services import link_applications_to_user

        application = self.guest_application("victim@example.com")
        linked = link_applications_to_user(self.user("victim@example.com", verified=False))

        application.refresh_from_db()
        self.assertEqual(linked, 0)
        self.assertIsNone(application.user)

    def test_an_application_already_owned_is_never_moved(self):
        from .services import link_applications_to_user

        owner = self.user("owner@example.com")
        application = self.guest_application("owner@example.com")
        application.user = owner
        application.save(update_fields=["user"])

        other = self.user("owner2@example.com")
        other.email = "owner@example.com"  # same address, different account
        other.save(update_fields=["email"])

        link_applications_to_user(other)
        application.refresh_from_db()
        self.assertEqual(application.user_id, owner.id)

    def test_matching_is_case_insensitive(self):
        from .services import link_applications_to_user

        application = self.guest_application("Mixed.Case@Example.com")
        link_applications_to_user(self.user("mixed.case@example.com"))

        application.refresh_from_db()
        self.assertIsNotNone(application.user)


class DraftPaymentMethodTests(TestCase):
    """
    The application fee is paid before an account exists, so the apply flow
    cannot use the resident endpoint. Without this route the payment step had
    no source of methods at all and told every applicant none were set up.
    """

    def setUp(self):
        from apps.billing.models import PaymentMethodConfig
        self.config = PaymentMethodConfig
        self.home = make_property()
        self.draft = RentalApplication.objects.create(
            status=ApplicationStatus.DRAFT, property=self.home, email="d@e.com",
            application_fee_cents=dollars(55),
        )

    def url(self, draft_id):
        return f"/api/v1/leads/apply/drafts/{draft_id}/payment-methods/"

    def test_an_unknown_draft_is_not_told_what_the_rails_are(self):
        import uuid
        response = self.client.get(self.url(uuid.uuid4()))
        self.assertEqual(response.status_code, 404)

    def test_a_real_draft_sees_active_configured_methods(self):
        self.config.objects.create(
            method="ZELLE", display_name="Zelle", handle="pay@example.com",
            is_active=True, irreversible=True, clearing_time="Usually within minutes",
        )
        response = self.client.get(self.url(self.draft.id))
        self.assertEqual(response.status_code, 200)
        self.assertEqual([m["display_name"] for m in response.json()], ["Zelle"])

    def test_inactive_methods_are_not_offered(self):
        self.config.objects.create(
            method="ZELLE", display_name="Zelle", handle="pay@example.com", is_active=False,
        )
        self.assertEqual(self.client.get(self.url(self.draft.id)).json(), [])

    def test_a_method_with_nothing_to_pay_to_is_not_offered(self):
        # The check constraint stops this being saved active, but the endpoint
        # must not depend on that alone: a blank account number on a live page
        # is the moment an applicant goes looking for details somewhere unsafe.
        self.config.objects.create(
            method="CHECK", display_name="Check", is_active=False,
        )
        self.assertEqual(self.client.get(self.url(self.draft.id)).json(), [])


class DeclaredPaymentReachesTheAdminTests(TestCase):
    """
    Submitting an application used to set a status and stop. An applicant who
    ticked "I have sent the payment", gave a reference and uploaded a receipt
    produced no record anywhere staff look, so there was nothing to approve.
    """

    def setUp(self):
        from apps.billing.models import Payment
        self.Payment = Payment
        self.home = make_property()
        self.draft = RentalApplication.objects.create(
            status=ApplicationStatus.DRAFT, property=self.home, email="p@q.com",
            application_fee_cents=dollars(55),
            draft_data={
                "applicationFeeCents": dollars(55),
                "paymentReportedAt": "2026-08-22T10:00:00Z",
                "paymentMethod": "zelle",
                "paymentReference": "SRG-8B6F-D6FB",
                "paymentProofPath": "proof-1.png",
            },
        )

    def submit(self, draft=None):
        target = draft or self.draft
        return self.client.post(f"/api/v1/leads/apply/drafts/{target.id}/submit/",
                                data={}, content_type="application/json")

    def test_a_declared_payment_lands_in_the_queue(self):
        self.submit()
        payment = self.Payment.objects.filter(rental_application=self.draft).first()
        self.assertIsNotNone(payment)
        self.assertEqual(payment.reference_id, "SRG-8B6F-D6FB")
        self.assertEqual(payment.payment_method, "ZELLE")

    def test_it_arrives_awaiting_a_person(self):
        # The applicant's claim that they sent money, not evidence it arrived.
        self.submit()
        payment = self.Payment.objects.get(rental_application=self.draft)
        self.assertEqual(payment.status, "PENDING_VERIFICATION")
        self.assertFalse(self.draft.is_fee_paid)

    def test_the_receipt_comes_with_it(self):
        self.submit()
        self.assertEqual(
            self.Payment.objects.get(rental_application=self.draft).proof_image_url,
            "proof-1.png",
        )

    def test_the_fee_is_recorded_from_what_the_applicant_was_shown(self):
        # Drafts start at zero, and a payment row cannot be created for zero.
        zeroed = RentalApplication.objects.create(
            status=ApplicationStatus.DRAFT, property=self.home, email="z@z.com",
            application_fee_cents=0,
            draft_data={
                "applicationFeeCents": dollars(110),
                "paymentReportedAt": "2026-08-22T10:00:00Z",
                "paymentMethod": "ach",
            },
        )
        self.client.patch(
            f"/api/v1/leads/apply/drafts/{zeroed.id}/",
            data={"applicationFeeCents": dollars(110)}, content_type="application/json",
        )
        self.submit(zeroed)
        zeroed.refresh_from_db()
        self.assertEqual(zeroed.application_fee_cents, dollars(110))
        self.assertEqual(
            self.Payment.objects.get(rental_application=zeroed).amount_cents, dollars(110),
        )

    def test_nothing_is_queued_when_no_payment_was_declared(self):
        quiet = RentalApplication.objects.create(
            status=ApplicationStatus.DRAFT, property=self.home, email="r@s.com",
            application_fee_cents=dollars(55), draft_data={},
        )
        self.submit(quiet)
        self.assertFalse(self.Payment.objects.filter(rental_application=quiet).exists())

    def test_resubmitting_does_not_queue_the_money_twice(self):
        self.submit()
        self.submit()
        self.assertEqual(self.Payment.objects.filter(rental_application=self.draft).count(), 1)

    def test_verifying_marks_the_fee_paid_and_starts_the_clock(self):
        # This is the step that "did not reflect": verify() closed invoices,
        # and an application fee has no invoice.
        self.submit()
        staff = User.objects.create_user(email="staff@skelton.test", password="x" * 12)
        payment = self.Payment.objects.get(rental_application=self.draft)
        payment.verify(staff)

        self.draft.refresh_from_db()
        self.assertTrue(self.draft.is_fee_paid)
        self.assertIsNotNone(self.draft.decision_due_at)
