"""
Self-tour booking and viewings.

TWO STEPS, LEAD FIRST — the request is captured before any identity document is
asked for, so an abandoned verification still leaves a contactable lead.

GOVERNMENT ID IMAGES ARE THE MOST SENSITIVE FILES THIS SYSTEM HOLDS. They exist
to be looked at once and then deleted; `id_purged_at` records that so retention
is auditable rather than aspirational. A licence scan sitting in object storage
for three years is a breach waiting for an unrelated mistake.

ACCESS CODES ARE PHYSICAL KEYS. Issued only after approval, given an expiry, and
cleared once the window passes. A smart-lock code that never expires is a key
handed to everyone who ever toured the property.
"""

import secrets
import uuid
from datetime import timedelta

from django.db import models
from django.utils import timezone


class TourStatus(models.TextChoices):
    AWAITING_ID = "AWAITING_ID", "Awaiting ID"
    PENDING_REVIEW = "PENDING_REVIEW", "Pending review"
    APPROVED = "APPROVED", "Approved"
    REJECTED = "REJECTED", "Rejected"
    # Called off by the person who booked it. Kept apart from REJECTED, which
    # is a staff decision, so the queue does not read as us turning people away.
    CANCELLED = "CANCELLED", "Cancelled"


class TourRequest(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    # The capability that authorises an ID upload, so it must be unguessable.
    public_id = models.UUIDField(default=uuid.uuid4, unique=True, db_index=True, editable=False)
    lead = models.ForeignKey("crm.Lead", null=True, blank=True, on_delete=models.SET_NULL)
    property = models.ForeignKey("properties.Property", null=True, blank=True, on_delete=models.SET_NULL)
    full_name = models.CharField(max_length=200)
    email = models.EmailField()
    phone = models.CharField(max_length=20, blank=True, default="")
    preferred_date = models.DateField()
    preferred_time = models.CharField(max_length=20)
    tour_type = models.CharField(max_length=20, default="self-tour")
    notes = models.TextField(blank=True, default="")

    id_front_url = models.CharField(max_length=500, blank=True, default="")
    id_back_url = models.CharField(max_length=500, blank=True, default="")
    id_purged_at = models.DateTimeField(null=True, blank=True)

    status = models.CharField(
        max_length=16, choices=TourStatus.choices, default=TourStatus.AWAITING_ID, db_index=True,
    )
    reviewed_by = models.ForeignKey("accounts.User", null=True, blank=True, on_delete=models.SET_NULL)
    reviewed_at = models.DateTimeField(null=True, blank=True)
    rejection_reason = models.TextField(blank=True, default="")
    viewing = models.ForeignKey("scheduler.Viewing", null=True, blank=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "tour_requests"
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.full_name} — {self.get_status_display()}"

    @classmethod
    def ready_to_purge(cls, older_than_hours: int = 24):
        """Reviewed requests whose documents have served their purpose."""
        cutoff = timezone.now() - timedelta(hours=older_than_hours)
        return cls.objects.filter(
            status__in=[TourStatus.APPROVED, TourStatus.REJECTED, TourStatus.CANCELLED],
            reviewed_at__isnull=False, reviewed_at__lt=cutoff, id_purged_at__isnull=True,
        ).exclude(id_front_url="", id_back_url="")

    def purge_ids(self) -> None:
        """
        Delete the ID images and record that we did.

        THE FILES ARE REMOVED, NOT JUST THE REFERENCES. This used to blank the
        two URL columns and stop there, which reads as a purge in the admin
        and in the database while every uploaded government ID stays on disk
        for ever, now unreferenced and so invisible to anyone reviewing what
        we hold. For this class of document that is the worst of both: the
        retention policy looks satisfied and nothing has actually been
        deleted.

        Unlinking is best-effort. A file that has already gone is the outcome
        we wanted, and a file we cannot remove must not stop the record being
        marked - otherwise one stuck file blocks the whole purge run.
        """
        from .storage import delete_tour_id

        for stored in (self.id_front_url, self.id_back_url):
            if stored:
                delete_tour_id(stored)

        self.id_front_url = ""
        self.id_back_url = ""
        self.id_purged_at = timezone.now()
        self.save(update_fields=["id_front_url", "id_back_url", "id_purged_at"])


class ViewingStatus(models.TextChoices):
    SCHEDULED = "SCHEDULED", "Scheduled"
    CONFIRMED = "CONFIRMED", "Confirmed"
    COMPLETED = "COMPLETED", "Completed"
    CANCELLED = "CANCELLED", "Cancelled"
    NO_SHOW = "NO_SHOW", "No show"


ACTIVE_VIEWING_STATUSES = [ViewingStatus.SCHEDULED, ViewingStatus.CONFIRMED]


class Viewing(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    lead = models.ForeignKey("crm.Lead", null=True, blank=True, on_delete=models.SET_NULL)
    property = models.ForeignKey("properties.Property", null=True, blank=True, on_delete=models.SET_NULL)
    agent = models.ForeignKey("accounts.User", null=True, blank=True, on_delete=models.SET_NULL)
    scheduled_at = models.DateTimeField(db_index=True)
    status = models.CharField(max_length=12, choices=ViewingStatus.choices, default=ViewingStatus.SCHEDULED)
    lease_term = models.CharField(max_length=40, blank=True, default="")
    access_code = models.CharField(max_length=10, blank=True, default="")
    access_code_expires_at = models.DateTimeField(null=True, blank=True)
    notes = models.TextField(blank=True, default="")
    reminder_24h_sent = models.BooleanField(default=False)
    reminder_2h_sent = models.BooleanField(default=False)
    confirmation_sent = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "viewings"
        ordering = ["scheduled_at"]

    def __str__(self) -> str:
        return f"Viewing {self.scheduled_at:%Y-%m-%d %H:%M}"

    @staticmethod
    def generate_access_code() -> str:
        """CSPRNG. A guessable door code is an unlocked door."""
        return f"{secrets.randbelow(1_000_000):06d}"

    def issue_access_code(self) -> str:
        self.access_code = self.generate_access_code()
        # Two hours past the slot. A code outliving its viewing is a key handed
        # to everyone who ever toured.
        self.access_code_expires_at = self.scheduled_at + timedelta(hours=2)
        self.save(update_fields=["access_code", "access_code_expires_at"])
        return self.access_code

    @classmethod
    def expire_access_codes(cls) -> int:
        return cls.objects.filter(
            access_code_expires_at__lt=timezone.now(),
        ).exclude(access_code="").update(access_code="")

    @classmethod
    def needing_reminder(cls, window: str):
        hours = 24 if window == "24h" else 2
        field = "reminder_24h_sent" if window == "24h" else "reminder_2h_sent"
        now = timezone.now()
        return cls.objects.filter(
            status__in=ACTIVE_VIEWING_STATUSES,
            scheduled_at__gt=now, scheduled_at__lte=now + timedelta(hours=hours),
            **{field: False},
        )
