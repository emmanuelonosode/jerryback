from datetime import timedelta

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone

from apps.accounts.models import User
from apps.core.money import dollars

from .models import (
    Invoice, InvoiceSequence, InvoiceStatus, Payment, PaymentMethodConfig,
    PaymentMethodKind, PaymentStatus,
)


def line(description, cents, quantity=1):
    return {"description": description, "quantity": quantity, "unit_price_cents": cents}


class InvoiceNumberingTests(TestCase):
    def test_is_sequential_within_a_year_and_zero_padded(self):
        self.assertEqual(InvoiceSequence.allocate(2026), "INV-2026-0001")
        self.assertEqual(InvoiceSequence.allocate(2026), "INV-2026-0002")

    def test_restarts_per_year_without_colliding(self):
        InvoiceSequence.allocate(2026)
        self.assertEqual(InvoiceSequence.allocate(2027), "INV-2027-0001")
        self.assertEqual(InvoiceSequence.allocate(2026), "INV-2026-0002")

    def test_never_issues_the_same_number_twice(self):
        # COUNT(*) + 1 hands two concurrent requests the same number. The
        # locked counter makes allocation atomic; 300 allocations here catch
        # any off-by-one or read-then-write gap as a duplicate.
        issued = {InvoiceSequence.allocate(2026) for _ in range(300)}
        self.assertEqual(len(issued), 300)


class InvoiceTotalTests(TestCase):
    def test_totals_are_computed_not_supplied(self):
        # An invoice whose stored total disagrees with the sum of its own lines
        # is indefensible in front of the person paying it.
        invoice = Invoice.objects.create(
            title="Move-in", due_date=timezone.localdate() + timedelta(days=7),
            line_items=[
                line("First month", dollars(1200)),
                line("Deposit", dollars(1200)),
                line("Admin fee", dollars(150)),
            ],
        )
        self.assertEqual(invoice.subtotal_cents, dollars(2550))
        self.assertEqual(invoice.total_cents, dollars(2550))

    def test_the_lines_sum_exactly_to_the_total_with_tax(self):
        invoice = Invoice.objects.create(
            title="Odd", due_date=timezone.localdate(),
            line_items=[line("a", 33_333, 3), line("b", 1_111, 7)],
            tax_basis_points=825,
        )
        summed = sum(item["total_cents"] for item in invoice.line_items)
        self.assertEqual(summed, invoice.subtotal_cents)
        self.assertEqual(invoice.subtotal_cents + invoice.tax_amount_cents, invoice.total_cents)

    def test_quantity_multiplies(self):
        invoice = Invoice.objects.create(
            title="Rent upfront", due_date=timezone.localdate(),
            line_items=[line("Rent", dollars(1200), 3)],
        )
        self.assertEqual(invoice.line_items[0]["total_cents"], dollars(3600))

    def test_a_paid_invoice_cannot_be_voided(self):
        # Reversing received money is a refund with its own trail, not an edit
        # that makes the original disappear.
        invoice = Invoice.objects.create(
            title="A", due_date=timezone.localdate(), line_items=[line("x", 100)],
            status=InvoiceStatus.PAID,
        )
        with self.assertRaises(ValidationError):
            invoice.void()


class PaymentMethodTests(TestCase):
    def test_a_method_cannot_go_live_with_nothing_to_pay_to(self):
        # Without details the payment step renders a blank where an account
        # number belongs, and the applicant asks through a channel we do not
        # control — the scam pattern exactly.
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                PaymentMethodConfig.objects.create(
                    method=PaymentMethodKind.CASHAPP, display_name="Cash App", is_active=True,
                )

    def test_clean_gives_a_showable_error_before_the_constraint_fires(self):
        config = PaymentMethodConfig(
            method=PaymentMethodKind.CASHAPP, display_name="Cash App", is_active=True,
        )
        with self.assertRaises(ValidationError):
            config.clean()

    def test_a_configured_active_method_is_payable_and_carries_its_warning(self):
        config = PaymentMethodConfig.objects.create(
            method=PaymentMethodKind.ZELLE, display_name="Zelle", handle="pay@example.com",
            is_active=True, irreversible=True,
        )
        self.assertTrue(config.is_payable)
        self.assertTrue(config.irreversible)

    def test_an_inactive_method_is_not_payable(self):
        config = PaymentMethodConfig.objects.create(
            method=PaymentMethodKind.ZELLE, display_name="Zelle", handle="pay@example.com",
        )
        self.assertFalse(config.is_payable)

    def test_no_card_columns_exist(self):
        # Storing a PAN pulls the whole service into PCI DSS scope, which the
        # manual-rails decision exists to avoid.
        fields = {f.name for f in Payment._meta.get_fields() if hasattr(f, "attname")}
        for banned in ("card_number", "card_expiry", "cardholder_name", "billing_address"):
            self.assertNotIn(banned, fields)

    def test_references_are_not_sequential(self):
        # A guessable reference lets someone probe for other applications.
        first = Payment.build_reference("09baa09c-f381-4eed-a4a1-4432bf725f8f")
        second = Payment.build_reference("11111111-2222-3333-4444-555555555555")
        self.assertRegex(first, r"^SRG-[A-Z0-9]{4}-[A-Z0-9]{4}$")
        self.assertNotEqual(first, second)


class PaymentVerificationTests(TestCase):
    def setUp(self):
        self.actor = User.objects.create_user(
            "acct@x.com", "correct horse battery", first_name="A", last_name="C", role="ACCOUNTANT",
        )
        self.invoice = Invoice.objects.create(
            title="Move-in", due_date=timezone.localdate() + timedelta(days=7),
            line_items=[line("Total", dollars(2550))],
        )

    def payment(self, cents, **over):
        return Payment.objects.create(
            amount_cents=cents, payment_method=PaymentMethodKind.CASHAPP,
            invoice=self.invoice, **over,
        )

    def test_verifying_records_who_and_when(self):
        # Without this there is no answer to "who marked this paid" after a
        # dispute, and the manual model depends on that answer existing.
        payment = self.payment(dollars(2550))
        payment.verify(self.actor)
        payment.refresh_from_db()
        self.assertEqual(payment.verified_by, self.actor)
        self.assertIsNotNone(payment.verified_at)

    def test_verifying_the_full_amount_closes_the_invoice(self):
        self.assertTrue(self.payment(dollars(2550)).verify(self.actor))
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.status, InvoiceStatus.PAID)

    def test_a_part_payment_does_not_close_the_invoice(self):
        # Otherwise a balance silently disappears.
        self.assertFalse(self.payment(dollars(1000)).verify(self.actor))
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.status, InvoiceStatus.DRAFT)

        # The balance closes it.
        self.assertTrue(self.payment(dollars(1550)).verify(self.actor))
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.status, InvoiceStatus.PAID)

    def test_a_rejected_payment_does_not_count_toward_the_balance(self):
        rejected = self.payment(dollars(2550))
        rejected.reject(self.actor, "Never arrived")
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.received_cents, 0)
        self.assertEqual(self.invoice.status, InvoiceStatus.DRAFT)

    def test_rejection_requires_a_reason(self):
        # "Rejected", unexplained, about money already sent, is the worst
        # message this system could send.
        payment = self.payment(dollars(100))
        with self.assertRaises(ValidationError):
            payment.reject(self.actor, "   ")

    def test_a_decision_cannot_be_made_twice(self):
        payment = self.payment(dollars(100))
        payment.verify(self.actor)
        with self.assertRaises(ValidationError):
            payment.verify(self.actor)
        with self.assertRaises(ValidationError):
            payment.reject(self.actor, "changed my mind")

    def test_the_database_refuses_a_decision_with_no_actor(self):
        # Belt and braces: the model methods always set it, but a bulk update
        # or a shell edit would not.
        payment = self.payment(dollars(100))
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Payment.objects.filter(pk=payment.pk).update(status=PaymentStatus.VERIFIED)

    def test_a_zero_or_negative_amount_is_refused(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                self.payment(0)


# ===========================================================================
# Resident portal API
#
# Money endpoints, so these are almost entirely authorisation and arithmetic:
# can a resident see another resident's rent, and can they mark their own rent
# paid. Both answers must stay no.
# ===========================================================================

from django.test import override_settings  # noqa: E402
from django.urls import reverse  # noqa: E402
from rest_framework.test import APIClient  # noqa: E402

from apps.accounts.models import Role as _Role, User as _User  # noqa: E402

_TEST_SECRET = "a-test-jwt-secret-that-is-long-enough-32"
_API_SETTINGS = {
    "DEFAULT_AUTHENTICATION_CLASSES": ["apps.accounts.authentication.JWTAuthentication"],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAuthenticated"],
}


@override_settings(JWT_SECRET=_TEST_SECRET, REST_FRAMEWORK=_API_SETTINGS)
class ResidentBillingApiTests(TestCase):
    def setUp(self):
        self.api = APIClient()
        self.user = _User.objects.create_user(
            email="ada@example.com", password="correct horse battery staple",
            first_name="Ada", last_name="Lovelace", role=_Role.CLIENT,
        )
        self.other = _User.objects.create_user(
            email="grace@example.com", password="correct horse battery staple",
            first_name="Grace", last_name="Hopper", role=_Role.CLIENT,
        )

    def auth(self, user):
        from apps.accounts import jwt as jwt_codec

        token = jwt_codec.encode(subject=str(user.pk), role=user.role, token_type="access")
        self.api.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")

    def make_invoice(self, user, total=185000, status=InvoiceStatus.SENT, **over):
        from django.utils import timezone

        return Invoice.objects.create(
            user=user, title="September rent", due_date=timezone.localdate(),
            line_items=[{"description": "Rent", "quantity": 1, "unit_price_cents": total}],
            status=status, **over,
        )

    def active_method(self, method=PaymentMethodKind.ZELLE):
        return PaymentMethodConfig.objects.create(
            method=method, display_name="Zelle", handle="pay@example.com", is_active=True,
        )

    # ---- authorisation ---------------------------------------------------

    def test_anonymous_callers_cannot_read_invoices(self):
        self.assertEqual(self.api.get(reverse("my-invoices")).status_code, 401)

    def test_a_resident_never_sees_another_residents_invoices(self):
        self.make_invoice(self.other, total=999900)
        mine = self.make_invoice(self.user)

        self.auth(self.user)
        body = self.api.get(reverse("my-invoices")).json()

        self.assertEqual([i["id"] for i in body], [str(mine.id)])

    def test_draft_invoices_are_not_shown_to_the_resident(self):
        self.make_invoice(self.user, status=InvoiceStatus.DRAFT)
        self.auth(self.user)
        self.assertEqual(self.api.get(reverse("my-invoices")).json(), [])

    def test_bank_details_are_not_served_to_anonymous_callers(self):
        self.active_method()
        self.assertEqual(self.api.get(reverse("payment-config")).status_code, 401)

    def test_inactive_payment_methods_are_never_listed(self):
        PaymentMethodConfig.objects.create(
            method=PaymentMethodKind.CASHAPP, display_name="Cash App",
            handle="$example", is_active=False,
        )
        self.auth(self.user)
        self.assertEqual(self.api.get(reverse("payment-config")).json(), [])

    # ---- submitting proof ------------------------------------------------

    def test_proof_against_someone_elses_invoice_is_refused(self):
        theirs = self.make_invoice(self.other)
        self.active_method()

        self.auth(self.user)
        response = self.api.post(reverse("submit-proof"), {
            "invoice": str(theirs.id), "amount_cents": 185000,
            "payment_method": PaymentMethodKind.ZELLE, "reference_id": "ABC123",
        }, format="json")

        self.assertEqual(response.status_code, 404)
        self.assertEqual(Payment.objects.count(), 0)

    def test_submitting_proof_records_a_claim_and_does_not_mark_the_invoice_paid(self):
        invoice = self.make_invoice(self.user)
        self.active_method()

        self.auth(self.user)
        response = self.api.post(reverse("submit-proof"), {
            "invoice": str(invoice.id), "amount_cents": 185000,
            "payment_method": PaymentMethodKind.ZELLE, "reference_id": "ABC123",
        }, format="json")

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["status"], PaymentStatus.PENDING_VERIFICATION)
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, InvoiceStatus.SENT)

    def test_proof_with_neither_reference_nor_screenshot_is_refused(self):
        invoice = self.make_invoice(self.user)
        self.active_method()

        self.auth(self.user)
        response = self.api.post(reverse("submit-proof"), {
            "invoice": str(invoice.id), "amount_cents": 185000,
            "payment_method": PaymentMethodKind.ZELLE,
        }, format="json")

        self.assertEqual(response.status_code, 400)

    def test_an_inactive_rail_cannot_be_used_to_submit_proof(self):
        invoice = self.make_invoice(self.user)
        self.auth(self.user)
        response = self.api.post(reverse("submit-proof"), {
            "invoice": str(invoice.id), "amount_cents": 185000,
            "payment_method": PaymentMethodKind.CASHAPP, "reference_id": "ABC123",
        }, format="json")

        self.assertEqual(response.status_code, 400)

    # ---- summary arithmetic ----------------------------------------------

    def test_summary_counts_only_verified_payments_as_paid(self):
        invoice = self.make_invoice(self.user, total=100000)
        Payment.objects.create(
            invoice=invoice, amount_cents=40000,
            payment_method=PaymentMethodKind.ZELLE, status=PaymentStatus.PENDING_VERIFICATION,
        )

        self.auth(self.user)
        body = self.api.get(reverse("billing-summary")).json()

        self.assertEqual(body["total_paid_cents"], 0)
        self.assertEqual(body["open_balance_cents"], 100000)

    def test_a_verified_part_payment_reduces_the_open_balance(self):
        from django.utils import timezone

        invoice = self.make_invoice(self.user, total=100000)
        Payment.objects.create(
            invoice=invoice, amount_cents=40000, payment_method=PaymentMethodKind.ZELLE,
            status=PaymentStatus.VERIFIED, verified_by=self.other, verified_at=timezone.now(),
        )

        self.auth(self.user)
        body = self.api.get(reverse("billing-summary")).json()

        self.assertEqual(body["total_paid_cents"], 40000)
        self.assertEqual(body["open_balance_cents"], 60000)

    def test_an_overpaid_invoice_reports_a_zero_balance_not_a_negative_one(self):
        from django.utils import timezone

        invoice = self.make_invoice(self.user, total=100000)
        Payment.objects.create(
            invoice=invoice, amount_cents=120000, payment_method=PaymentMethodKind.ZELLE,
            status=PaymentStatus.VERIFIED, verified_by=self.other, verified_at=timezone.now(),
        )

        self.auth(self.user)
        self.assertEqual(self.api.get(reverse("billing-summary")).json()["open_balance_cents"], 0)
        self.assertEqual(self.api.get(reverse("my-invoices")).json()[0]["balance_cents"], 0)
