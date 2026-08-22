"""
Billing: payment method configuration, invoices, payments.

INVOICE NUMBERS COME FROM A LOCKED COUNTER ROW, NOT COUNT(*) + 1.

The obvious implementation issues the same number to two concurrent requests.
`invoice_number` is unique, so one fails with a constraint error that looks like
a bug — and if uniqueness were ever relaxed, two invoices would silently share a
number and the books would be wrong for months. `select_for_update` on a counter
row makes allocation atomic: the second writer blocks and gets the next value.

NO CARD COLUMNS. The spec carries card_number, card_expiry, cardholder_name and
billing_address here. Storing a PAN pulls the whole service into PCI DSS scope,
which is precisely what the manual-rails decision was made to avoid. If card
payment is added later it goes through a processor that returns a token, and the
token is what lands here.
"""

import uuid

from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.utils import timezone


class PaymentMethodKind(models.TextChoices):
    """
    Every rail the business collects on.

    GROUPED BY WHAT THEY COST THE PAYER IF SOMETHING GOES WRONG, because that
    is what the payment page has to communicate and it is not obvious from the
    brand name. Bank rails can be recalled; peer-to-peer apps and crypto cannot.
    """

    # Bank rails - traceable, and a mistaken payment can usually be recalled.
    ACH = "ACH", "ACH transfer"
    WIRE = "WIRE", "Wire transfer"
    DIRECT_DEPOSIT = "DIRECT_DEPOSIT", "Direct deposit"
    BANK_TRANSFER = "BANK_TRANSFER", "Bank transfer"
    CHECK = "CHECK", "Check or money order"

    # Peer-to-peer. Irreversible once sent, and the rails rental fraud runs on.
    ZELLE = "ZELLE", "Zelle"
    VENMO = "VENMO", "Venmo"
    CASHAPP = "CASHAPP", "Cash App"
    CHIME = "CHIME", "Chime"
    PAYPAL = "PAYPAL", "PayPal"
    APPLE_PAY = "APPLE_PAY", "Apple Pay"

    # Crypto. Irreversible, and the amount moves against the dollar between
    # sending and confirmation - see the note on `PaymentMethodConfig`.
    LITECOIN = "LITECOIN", "Litecoin"
    SOLANA = "SOLANA", "Solana"

    OTHER = "OTHER", "Something else"


#: Rails that hand the payer no recourse once the money is gone.
IRREVERSIBLE_KINDS = {
    PaymentMethodKind.ZELLE,
    PaymentMethodKind.VENMO,
    PaymentMethodKind.CASHAPP,
    PaymentMethodKind.CHIME,
    PaymentMethodKind.APPLE_PAY,
    PaymentMethodKind.LITECOIN,
    PaymentMethodKind.SOLANA,
}

#: Rails whose dollar value is not fixed at the moment of sending.
CRYPTO_KINDS = {PaymentMethodKind.LITECOIN, PaymentMethodKind.SOLANA}


class PaymentMethodConfig(models.Model):
    """
    Manual rails, per the product decision already made.

    The benefit: no card data touches this system — no PCI scope, no processor,
    no stored credentials. The risk, which shapes the rest: Zelle, Chime and
    Cash App are the rails rental fraud runs on. They are irreversible and
    effectively untraceable, which is why scammers ask for them and why renters
    are taught to treat the request as a red flag. So details are published only
    on the site, behind an application the applicant started, and never sent by
    email or text.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    method = models.CharField(max_length=20, choices=PaymentMethodKind.choices, unique=True)
    display_name = models.CharField(max_length=50)
    handle = models.CharField(max_length=200, blank=True, default="")
    is_active = models.BooleanField(default=False)
    extra_instructions = models.TextField(blank=True, default="")
    # True where the rail gives the payer no recourse once sent. Drives the
    # warning shown beside the method.
    irreversible = models.BooleanField(default=False)
    clearing_time = models.CharField(max_length=60, blank=True, default="")

    recipient_name = models.CharField(max_length=200, blank=True, default="")
    bank_name = models.CharField(max_length=200, blank=True, default="")
    account_type = models.CharField(max_length=40, blank=True, default="")
    account_number = models.CharField(max_length=64, blank=True, default="")
    routing_number = models.CharField(max_length=32, blank=True, default="")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "payment_method_configs"
        constraints = [
            # A method cannot go live with nothing to pay to. Without this the
            # payment step renders a blank where an account number belongs, and
            # the applicant either gives up or asks for details through a
            # channel we do not control — the scam pattern exactly.
            models.CheckConstraint(
                condition=models.Q(is_active=False) | ~models.Q(handle="") | ~models.Q(account_number=""),
                name="active_method_has_details",
            ),
        ]

    def __str__(self) -> str:
        return self.display_name

    def clean(self):
        if self.is_active and not (self.handle.strip() or self.account_number.strip()):
            raise ValidationError("A payment method cannot be activated without a handle or account number.")

    @property
    def is_payable(self) -> bool:
        return self.is_active and bool(self.handle.strip() or self.account_number.strip())


class InvoiceSequence(models.Model):
    """One row per year. Locked during allocation — see the module docstring."""

    year = models.PositiveIntegerField(primary_key=True)
    last_number = models.PositiveIntegerField(default=0)

    class Meta:
        db_table = "invoice_sequences"

    @classmethod
    def allocate(cls, year: int) -> str:
        with transaction.atomic():
            row, _ = cls.objects.select_for_update().get_or_create(year=year)
            row.last_number += 1
            row.save(update_fields=["last_number"])
            return f"INV-{year}-{row.last_number:04d}"


class InvoiceStatus(models.TextChoices):
    DRAFT = "DRAFT", "Draft"
    SENT = "SENT", "Sent"
    PAID = "PAID", "Paid"
    VOID = "VOID", "Void"


class Invoice(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    invoice_number = models.CharField(max_length=20, unique=True, db_index=True)
    user = models.ForeignKey("accounts.User", null=True, blank=True, on_delete=models.SET_NULL)
    rental_application = models.ForeignKey(
        "crm.RentalApplication", null=True, blank=True, on_delete=models.SET_NULL, related_name="invoices",
    )
    title = models.CharField(max_length=200)
    description = models.TextField(blank=True, default="")
    issued_date = models.DateField(default=timezone.localdate)
    due_date = models.DateField()
    # [{description, quantity, unit_price_cents, total_cents}]
    line_items = models.JSONField(default=list)
    subtotal_cents = models.BigIntegerField(default=0)
    tax_basis_points = models.PositiveIntegerField(default=0)
    tax_amount_cents = models.BigIntegerField(default=0)
    total_cents = models.BigIntegerField(default=0)
    pdf_url = models.CharField(max_length=500, blank=True, default="")
    status = models.CharField(max_length=8, choices=InvoiceStatus.choices, default=InvoiceStatus.DRAFT)
    due_reminder_sent = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "invoices"
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return self.invoice_number

    def recalculate(self) -> None:
        """
        Totals are COMPUTED, never accepted from a caller.

        An invoice whose stored total disagrees with the sum of its own lines is
        indefensible in front of the person paying it.
        """
        from apps.core.money import basis_points_of

        priced = []
        for item in self.line_items:
            qty = int(item.get("quantity", 1))
            unit = int(item["unit_price_cents"])
            priced.append({**item, "quantity": qty, "unit_price_cents": unit, "total_cents": unit * qty})
        self.line_items = priced
        self.subtotal_cents = sum(i["total_cents"] for i in priced)
        self.tax_amount_cents = basis_points_of(self.subtotal_cents, self.tax_basis_points)
        self.total_cents = self.subtotal_cents + self.tax_amount_cents

    def save(self, *args, **kwargs):
        if not self.invoice_number:
            self.invoice_number = InvoiceSequence.allocate(timezone.now().year)
        self.recalculate()
        super().save(*args, **kwargs)

    def void(self) -> None:
        # Reversing received money is a refund with its own audit trail, not an
        # edit that makes the original disappear.
        if self.status == InvoiceStatus.PAID:
            raise ValidationError("A paid invoice cannot be voided. Record a refund instead.")
        self.status = InvoiceStatus.VOID
        self.save(update_fields=["status", "updated_at"])

    @property
    def received_cents(self) -> int:
        return self.payments.filter(status=PaymentStatus.VERIFIED).aggregate(
            total=models.Sum("amount_cents"),
        )["total"] or 0


class PaymentStatus(models.TextChoices):
    PENDING = "PENDING", "Pending"
    PENDING_VERIFICATION = "PENDING_VERIFICATION", "Awaiting verification"
    VERIFIED = "VERIFIED", "Verified"
    REJECTED = "REJECTED", "Rejected"
    REFUNDED = "REFUNDED", "Refunded"


class Payment(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    invoice = models.ForeignKey(Invoice, null=True, blank=True, on_delete=models.SET_NULL, related_name="payments")
    rental_application = models.ForeignKey(
        "crm.RentalApplication", null=True, blank=True, on_delete=models.SET_NULL, related_name="payments",
    )
    amount_cents = models.BigIntegerField()
    payment_method = models.CharField(max_length=20, choices=PaymentMethodKind.choices)
    status = models.CharField(
        max_length=24, choices=PaymentStatus.choices, default=PaymentStatus.PENDING_VERIFICATION, db_index=True,
    )
    # What the payer put in the memo, so a person can reconcile it.
    reference_id = models.CharField(max_length=60, blank=True, default="")
    proof_image_url = models.CharField(max_length=500, blank=True, default="")
    verified_by = models.ForeignKey("accounts.User", null=True, blank=True, on_delete=models.SET_NULL)
    verified_at = models.DateTimeField(null=True, blank=True)
    rejection_reason = models.TextField(blank=True, default="")
    paid_at = models.DateTimeField(null=True, blank=True)
    receipt_sent = models.BooleanField(default=False)
    notes = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "payments"
        ordering = ["created_at"]
        constraints = [
            models.CheckConstraint(condition=models.Q(amount_cents__gt=0), name="payment_amount_positive"),
            # Without who and when there is no answer to "who marked this paid"
            # after a dispute, and the manual model depends on that answer.
            models.CheckConstraint(
                condition=(
                    ~models.Q(status__in=["VERIFIED", "REJECTED"])
                    | (models.Q(verified_by__isnull=False) & models.Q(verified_at__isnull=False))
                ),
                name="decided_payment_records_actor",
            ),
            # "Rejected", unexplained, about money already sent, is the worst
            # message this system could send.
            models.CheckConstraint(
                condition=~models.Q(status="REJECTED") | ~models.Q(rejection_reason=""),
                name="rejected_payment_has_reason",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.amount_cents}c via {self.payment_method} ({self.status})"

    @staticmethod
    def build_reference(record_id) -> str:
        """
        Not sequential: a guessable reference lets someone probe for other
        people's applications. Formatted to survive being read off a screen and
        typed into a bank app.
        """
        compact = str(record_id).replace("-", "").upper()
        return f"SRG-{compact[:4]}-{compact[4:8]}"

    def verify(self, actor) -> bool:
        """Confirm the money arrived. Returns whether it closed an invoice."""
        if self.status in (PaymentStatus.VERIFIED, PaymentStatus.REJECTED):
            raise ValidationError("This payment has already been decided.")
        now = timezone.now()
        self.status = PaymentStatus.VERIFIED
        self.verified_by = actor
        self.verified_at = now
        self.paid_at = now
        self.save(update_fields=["status", "verified_by", "verified_at", "paid_at", "updated_at"])

        # AN APPLICATION FEE HAS NO INVOICE, so the branch below never fired for
        # one and verifying it changed nothing an applicant or an agent could
        # see: `is_fee_paid` stayed false, the 24-hour clock never started, and
        # staff reported confirming a payment that "did not reflect". The
        # application owns that transition, so it is asked to make it.
        if self.rental_application and not self.rental_application.is_fee_paid:
            self.rental_application.start_decision_clock()

        if self.invoice and self.invoice.received_cents >= self.invoice.total_cents:
            # A PART payment must not close an invoice: that is how a balance
            # silently disappears.
            self.invoice.status = InvoiceStatus.PAID
            self.invoice.save(update_fields=["status", "updated_at"])
            return True
        return False

    def reject(self, actor, reason: str) -> None:
        if not reason.strip():
            raise ValidationError("Rejecting a payment requires a reason the applicant can be told.")
        if self.status in (PaymentStatus.VERIFIED, PaymentStatus.REJECTED):
            raise ValidationError("This payment has already been decided.")
        self.status = PaymentStatus.REJECTED
        self.verified_by = actor
        self.verified_at = timezone.now()
        self.rejection_reason = reason.strip()
        self.save(update_fields=[
            "status", "verified_by", "verified_at", "rejection_reason", "updated_at",
        ])
