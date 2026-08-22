"""Tenant portal: documents and maintenance tickets."""

import uuid

from django.db import models
from django.utils import timezone


class DocumentType(models.TextChoices):
    CONTRACT = "CONTRACT", "Contract"
    RECEIPT = "RECEIPT", "Receipt"
    AGREEMENT = "AGREEMENT", "Agreement"
    ID_DOCUMENT = "ID_DOCUMENT", "Identity document"
    PROOF_OF_FUNDS = "PROOF_OF_FUNDS", "Proof of funds"
    OTHER = "OTHER", "Other"


class ClientDocument(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    client = models.ForeignKey("crm.Client", on_delete=models.CASCADE, related_name="documents")
    name = models.CharField(max_length=200)
    file_url = models.CharField(max_length=500)
    document_type = models.CharField(max_length=20, choices=DocumentType.choices, default=DocumentType.OTHER)
    is_signed = models.BooleanField(default=False)
    uploaded_by = models.ForeignKey("accounts.User", null=True, blank=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "client_documents"
        ordering = ["-created_at"]


class MaintenanceCategory(models.TextChoices):
    PLUMBING = "PLUMBING", "Plumbing"
    ELECTRICAL = "ELECTRICAL", "Electrical"
    HVAC = "HVAC", "Heating and cooling"
    APPLIANCE = "APPLIANCE", "Appliance"
    STRUCTURAL = "STRUCTURAL", "Structural"
    PEST = "PEST", "Pest"
    SECURITY = "SECURITY", "Security"
    OTHER = "OTHER", "Other"


class MaintenancePriority(models.TextChoices):
    LOW = "LOW", "Low"
    MEDIUM = "MEDIUM", "Medium"
    HIGH = "HIGH", "High"
    URGENT = "URGENT", "Urgent"


class MaintenanceStatus(models.TextChoices):
    SUBMITTED = "SUBMITTED", "Submitted"
    ACKNOWLEDGED = "ACKNOWLEDGED", "Acknowledged"
    IN_PROGRESS = "IN_PROGRESS", "In progress"
    RESOLVED = "RESOLVED", "Resolved"
    CLOSED = "CLOSED", "Closed"


class MaintenanceRequest(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    client = models.ForeignKey("crm.Client", on_delete=models.CASCADE, related_name="maintenance_requests")
    property = models.ForeignKey("properties.Property", null=True, blank=True, on_delete=models.SET_NULL)
    title = models.CharField(max_length=200)
    description = models.TextField()
    category = models.CharField(max_length=16, choices=MaintenanceCategory.choices)
    priority = models.CharField(
        max_length=8, choices=MaintenancePriority.choices, default=MaintenancePriority.MEDIUM,
    )
    status = models.CharField(
        max_length=16, choices=MaintenanceStatus.choices, default=MaintenanceStatus.SUBMITTED, db_index=True,
    )
    photo_url = models.CharField(max_length=500, blank=True, default="")
    preferred_access_time = models.CharField(max_length=120, blank=True, default="")
    staff_notes = models.TextField(blank=True, default="")
    resolved_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "maintenance_requests"
        # Urgent first, then oldest: a habitability issue outranks a fresh
        # low-priority ticket.
        ordering = ["-priority", "created_at"]

    def __str__(self) -> str:
        return f"{self.title} ({self.get_status_display()})"

    def resolve(self, notes: str = "") -> None:
        self.status = MaintenanceStatus.RESOLVED
        self.resolved_at = timezone.now()
        if notes:
            self.staff_notes = notes
        self.save(update_fields=["status", "resolved_at", "staff_notes", "updated_at"])
