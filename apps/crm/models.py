"""
CRM: leads, activities, referrals, and rental applications.

TWO THINGS HERE ARE DELIBERATE AND EASY TO "FIX" WRONGLY.

LEAD SCORE IS NOT A COLUMN. The formula reads activity count, age in days, and
status, so a stored value is wrong the moment a day passes. Keeping it accurate
would mean recomputing on every write to three other tables, and ranking a
pipeline by a number that silently decays is worse than not ranking it, because
it looks authoritative. It is a property; see `scoring.py`.

THE FULL SSN IS NOT STORED, ENCRYPTED OR OTHERWISE. The spec stores a Fernet
ciphertext whose key is derived from SECRET_KEY by SHA-256. Two problems, the
second decisive:

  The key is the secret. A config leak, a backup, an error page or git history
  hands over every SSN in the table. That is encryption at rest against a threat
  model where the attacker never has the application config.

  Nothing needs it. The number exists to be handed to a screening vendor, which
  returns a report reference; the decision is made from the report. Once sent,
  retaining it adds breach liability and buys nothing.

So: last four for identification, a vendor reference for the report, and the
full number passes through memory to the vendor and is never written down. If a
specific retention obligation is ever identified, it belongs in a dedicated
store with a KMS-managed key and an audit log, not in an application row keyed
off the web server's secret.
"""

import secrets
import uuid
from datetime import timedelta

from django.db import models
from django.utils import timezone

# `RentalApplication` has a field named `property`, which shadows the builtin
# for the rest of the class body — so a later `@property` resolves to the
# ForeignKey and Django fails to import with "'ForeignKey' object is not
# callable". Aliasing the builtin first keeps both the natural field name
# (`application.property`) and computed properties on the same model.
computed = property


class LeadSource(models.TextChoices):
    CONTACT_FORM = "CONTACT_FORM", "Contact form"
    PROPERTY_INQUIRY = "PROPERTY_INQUIRY", "Property inquiry"
    AGENT_INQUIRY = "AGENT_INQUIRY", "Agent inquiry"
    REFERRAL = "REFERRAL", "Referral"
    GOOGLE = "GOOGLE", "Google"
    INSTAGRAM = "INSTAGRAM", "Instagram"
    FACEBOOK = "FACEBOOK", "Facebook"
    DIRECT = "DIRECT", "Direct"


class LeadStatus(models.TextChoices):
    NEW = "NEW", "New"
    CONTACTED = "CONTACTED", "Contacted"
    QUALIFIED = "QUALIFIED", "Qualified"
    VIEWING = "VIEWING", "Viewing"
    NEGOTIATING = "NEGOTIATING", "Negotiating"
    CONVERTED = "CONVERTED", "Converted"
    LOST = "LOST", "Lost"


class MoveInTimeline(models.TextChoices):
    ASAP = "ASAP", "As soon as possible"
    ONE_TO_THREE = "1_3_MONTHS", "1-3 months"
    THREE_TO_SIX = "3_6_MONTHS", "3-6 months"
    SIX_PLUS = "6_PLUS", "6+ months"
    BROWSING = "JUST_BROWSING", "Just browsing"


class Lead(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    full_name = models.CharField(max_length=200)
    email = models.EmailField(db_index=True)
    phone = models.CharField(max_length=20, blank=True, default="")
    source = models.CharField(max_length=24, choices=LeadSource.choices, default=LeadSource.CONTACT_FORM)
    interest_type = models.CharField(max_length=8, default="RENT")

    budget_min_cents = models.BigIntegerField(null=True, blank=True)
    budget_max_cents = models.BigIntegerField(null=True, blank=True)
    preferred_location = models.CharField(max_length=200, blank=True, default="")
    property_interest = models.ForeignKey(
        "properties.Property", null=True, blank=True, on_delete=models.SET_NULL, related_name="inquiries",
    )
    message = models.TextField(blank=True, default="")

    utm_source = models.CharField(max_length=100, blank=True, default="")
    utm_medium = models.CharField(max_length=100, blank=True, default="")
    utm_campaign = models.CharField(max_length=200, blank=True, default="")
    detected_city = models.CharField(max_length=100, blank=True, default="")

    move_in_timeline = models.CharField(
        max_length=16, choices=MoveInTimeline.choices, blank=True, default="",
    )
    occupants_count = models.PositiveSmallIntegerField(null=True, blank=True)
    has_pets = models.BooleanField(null=True, blank=True)
    # Not in the supplied spec. The single fact that most changes how a lead is
    # handled here, and absent from both partner feeds as well.
    has_voucher = models.BooleanField(null=True, blank=True)
    preferred_contact = models.CharField(max_length=8, blank=True, default="")
    referral_code = models.CharField(max_length=20, blank=True, default="", db_index=True)
    drip_opted_out = models.BooleanField(default=False)

    status = models.CharField(
        max_length=16, choices=LeadStatus.choices, default=LeadStatus.NEW, db_index=True,
    )
    assigned_agent = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="assigned_leads",
    )
    last_contacted_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "leads"
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.full_name} <{self.email}>"

    def save(self, *args, **kwargs):
        self.email = self.email.strip().lower()
        self.full_name = self.full_name.strip()
        super().save(*args, **kwargs)

    @property
    def score(self) -> int:
        from .scoring import score_lead

        return score_lead(self)["score"]

    @property
    def score_detail(self) -> dict:
        from .scoring import score_lead

        return score_lead(self)


class ActivityType(models.TextChoices):
    CALL = "CALL", "Call"
    EMAIL = "EMAIL", "Email"
    NOTE = "NOTE", "Note"
    STATUS_CHANGE = "STATUS_CHANGE", "Status change"
    VIEWING_BOOKED = "VIEWING_BOOKED", "Viewing booked"
    EMAIL_OPENED = "EMAIL_OPENED", "Email opened"
    LINK_CLICKED = "LINK_CLICKED", "Link clicked"


# Only these count as a human having made contact. An automated open or click
# is a signal, not a conversation, and letting it move `last_contacted_at`
# suppresses the follow-up job for someone nobody has actually spoken to.
HUMAN_CONTACT = {ActivityType.CALL, ActivityType.EMAIL}


class LeadActivity(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    lead = models.ForeignKey(Lead, on_delete=models.CASCADE, related_name="activities")
    agent = models.ForeignKey("accounts.User", null=True, blank=True, on_delete=models.SET_NULL)
    activity_type = models.CharField(max_length=20, choices=ActivityType.choices)
    note = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "lead_activities"
        ordering = ["-created_at"]
        verbose_name_plural = "lead activities"

    def save(self, *args, **kwargs):
        created = self._state.adding
        super().save(*args, **kwargs)
        if created and self.activity_type in HUMAN_CONTACT:
            Lead.objects.filter(pk=self.lead_id).update(last_contacted_at=timezone.now())


class Client(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.OneToOneField("accounts.User", null=True, blank=True, on_delete=models.SET_NULL)
    lead = models.OneToOneField(Lead, null=True, blank=True, on_delete=models.SET_NULL)
    preferred_agent = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="clients",
    )
    kyc_verified = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "clients"

    def __str__(self) -> str:
        return str(self.user or self.lead or self.pk)


class Referrer(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=200)
    email = models.EmailField(unique=True)
    phone = models.CharField(max_length=20, blank=True, default="")
    # Random, not sequential: a guessable code lets anyone claim another
    # referrer's commission by trying values.
    code = models.CharField(max_length=20, unique=True, db_index=True)
    is_active = models.BooleanField(default=True)
    notes = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "referrers"

    def save(self, *args, **kwargs):
        if not self.code:
            self.code = secrets.token_hex(4)
        self.email = self.email.strip().lower()
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return f"{self.name} ({self.code})"


class PayoutStatus(models.TextChoices):
    PENDING = "PENDING", "Pending"
    CONVERTED = "CONVERTED", "Converted"
    PAID = "PAID", "Paid"
    VOID = "VOID", "Void"


class ReferralPayout(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    referrer = models.ForeignKey(Referrer, on_delete=models.CASCADE, related_name="payouts")
    lead = models.ForeignKey(Lead, null=True, blank=True, on_delete=models.SET_NULL)
    rental_application = models.ForeignKey(
        "crm.RentalApplication", null=True, blank=True, on_delete=models.SET_NULL,
    )
    status = models.CharField(max_length=12, choices=PayoutStatus.choices, default=PayoutStatus.PENDING)
    monthly_rent_cents = models.BigIntegerField()
    # Basis points: 4000 = 40%. The spec's 0.40 decimal times a rent produces
    # fractional cents on most rents, and a payout that does not reconcile to
    # the penny is a dispute with a person about money.
    commission_basis_points = models.PositiveIntegerField(default=4000)
    commission_months = models.PositiveSmallIntegerField(default=2)
    commission_amount_cents = models.BigIntegerField()
    notes = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    converted_at = models.DateTimeField(null=True, blank=True)
    paid_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "referral_payouts"
        ordering = ["-created_at"]

    def save(self, *args, **kwargs):
        from apps.core.money import basis_points_of

        self.commission_amount_cents = basis_points_of(
            self.monthly_rent_cents * self.commission_months, self.commission_basis_points,
        )
        super().save(*args, **kwargs)


class ApplicationStatus(models.TextChoices):
    DRAFT = "DRAFT", "Draft"
    PENDING_PAYMENT = "PENDING_PAYMENT", "Pending payment"
    PENDING_VERIFICATION = "PENDING_VERIFICATION", "Payment reported, awaiting verification"
    SUBMITTED = "SUBMITTED", "Submitted"
    REVIEWED = "REVIEWED", "Reviewed"
    APPROVED = "APPROVED", "Approved"
    APPROVED_WITH_CONDITIONS = "APPROVED_WITH_CONDITIONS", "Approved with conditions"
    REJECTED = "REJECTED", "Declined"


DECIDED_STATUSES = {
    ApplicationStatus.APPROVED,
    ApplicationStatus.APPROVED_WITH_CONDITIONS,
    ApplicationStatus.REJECTED,
}


class RentalApplication(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    status = models.CharField(
        max_length=28, choices=ApplicationStatus.choices, default=ApplicationStatus.DRAFT, db_index=True,
    )
    property = models.ForeignKey(
        "properties.Property", null=True, blank=True, on_delete=models.SET_NULL, related_name="applications",
    )
    lead = models.ForeignKey(Lead, null=True, blank=True, on_delete=models.SET_NULL)
    user = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="applications",
    )

    application_fee_cents = models.BigIntegerField()
    is_fee_paid = models.BooleanField(default=False)

    # Every applicant field is nullable: drafts are saved partially by design,
    # because abandonment is high and recovery is revenue.
    first_name = models.CharField(max_length=100, blank=True, default="")
    middle_name = models.CharField(max_length=100, blank=True, default="")
    last_name = models.CharField(max_length=100, blank=True, default="")
    email = models.EmailField(blank=True, default="", db_index=True)
    cell_phone = models.CharField(max_length=20, blank=True, default="")
    date_of_birth = models.DateField(null=True, blank=True)

    present_address = models.CharField(max_length=200, blank=True, default="")
    city = models.CharField(max_length=100, blank=True, default="")
    state = models.CharField(max_length=2, blank=True, default="")
    zip_code = models.CharField(max_length=10, blank=True, default="")
    how_long_at_address = models.CharField(max_length=100, blank=True, default="")
    reason_for_leaving = models.TextField(blank=True, default="")
    current_landlord_name = models.CharField(max_length=200, blank=True, default="")
    current_landlord_phone = models.CharField(max_length=20, blank=True, default="")

    move_in_date = models.DateField(null=True, blank=True)
    months_rent_upfront = models.PositiveSmallIntegerField(default=1)
    security_deposit_cents = models.BigIntegerField(null=True, blank=True)
    lease_admin_fee_cents = models.BigIntegerField(null=True, blank=True)
    pet_fee_cents = models.BigIntegerField(null=True, blank=True)

    id_type = models.CharField(max_length=40, blank=True, default="")
    # Identification only. See the module docstring for why the full number is
    # not here.
    ssn_last4 = models.CharField(max_length=4, blank=True, default="")
    screening_reference = models.CharField(max_length=120, blank=True, default="")
    ein = models.CharField(max_length=10, blank=True, default="")

    gross_monthly_income_cents = models.BigIntegerField(null=True, blank=True)
    employer_name = models.CharField(max_length=200, blank=True, default="")
    job_title = models.CharField(max_length=120, blank=True, default="")
    # Voucher income counts against the applicant's share only, never twice.
    voucher_covers_cents = models.BigIntegerField(null=True, blank=True)

    has_kids = models.BooleanField(null=True, blank=True)
    number_of_kids = models.PositiveSmallIntegerField(null=True, blank=True)
    has_pets = models.BooleanField(null=True, blank=True)
    animals = models.JSONField(default=list, blank=True)
    has_felony_eviction_bankruptcy = models.BooleanField(null=True, blank=True)
    is_active_military = models.BooleanField(null=True, blank=True)
    has_housing_assistance = models.BooleanField(null=True, blank=True)

    certification_text = models.CharField(max_length=500, blank=True, default="")
    application_pdf_url = models.CharField(max_length=500, blank=True, default="")
    recovery_email_sent = models.BooleanField(default=False)

    # The 24-hour clock starts HERE, at payment verification, not at submission.
    # With manual rails there is a real gap between sending money and a person
    # confirming it arrived; starting at submission advertises a deadline the
    # company begins missing on day one.
    verified_at = models.DateTimeField(null=True, blank=True)
    decision_due_at = models.DateTimeField(null=True, blank=True, db_index=True)
    decided_at = models.DateTimeField(null=True, blank=True)
    decision_reason = models.TextField(blank=True, default="")

    # The full in-progress draft, exactly as the application form holds it.
    #
    # WHY A JSON BLOB ALONGSIDE REAL COLUMNS. The form collects nested,
    # still-changing shapes — several income sources, prior addresses,
    # occupants, pets — that have no settled schema while someone is halfway
    # through typing them. The columns carry what staff need to SEE in the
    # admin (a name, an email, a phone) and are filled as soon as those steps
    # are done; this carries everything, so a half-finished application can be
    # handed back exactly as it was left.
    draft_data = models.JSONField(default=dict, blank=True)
    submitted_at = models.DateTimeField(null=True, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    utm_source = models.CharField(max_length=100, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True, db_index=True)

    class Meta:
        db_table = "rental_applications"
        ordering = ["-created_at"]
        verbose_name = "rental application"
        verbose_name_plural = "rental applications"

    def __str__(self) -> str:
        return f"{self.first_name} {self.last_name} — {self.get_status_display()}".strip()

    @computed
    def is_decided(self) -> bool:
        return self.status in DECIDED_STATUSES

    @computed
    def is_overdue(self) -> bool:
        return bool(self.decision_due_at and not self.decided_at and self.decision_due_at < timezone.now())

    def start_decision_clock(self, hours: int = 24) -> None:
        now = timezone.now()
        self.verified_at = now
        self.decision_due_at = now + timedelta(hours=hours)
        self.is_fee_paid = True
        self.status = ApplicationStatus.SUBMITTED
        self.save(update_fields=[
            "verified_at", "decision_due_at", "is_fee_paid", "status", "updated_at",
        ])


class AdverseActionNotice(models.Model):
    """
    FCRA §1681m(a).

    A notice is required when a decision is based wholly or partly on a consumer
    report — and the statute covers approval on terms LESS FAVOURABLE than those
    requested, not only outright denial. So the tier-two track, which approves
    people with a larger deposit or a co-signer because of what the report
    showed, generates notices on approvals. That reads as a contradiction until
    you notice the wording, which is exactly why it is enforced in code.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    rental_application = models.ForeignKey(
        RentalApplication, on_delete=models.CASCADE, related_name="adverse_action_notices",
    )
    reason = models.TextField()
    # Without the agency the applicant cannot dispute the report, so it is not
    # a notice.
    agency_name = models.CharField(max_length=200)
    agency_contact = models.CharField(max_length=200)
    sent_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "adverse_action_notices"
        ordering = ["-created_at"]
