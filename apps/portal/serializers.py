"""
Serializers for the resident portal.

WRITE FIELDS ARE ENUMERATED, NEVER INHERITED. Every create serializer here
lists `fields` explicitly and marks everything the resident must not control as
read-only. A `__all__` on MaintenanceRequest would let a resident post
`status: RESOLVED`, `staff_notes`, or another client's id in the same request
that creates the ticket — the classic mass-assignment hole, and on this model it
would let someone close their own habitability complaint.
"""

from rest_framework import serializers

from .models import (
    ClientDocument,
    MaintenanceCategory,
    MaintenancePriority,
    MaintenanceRequest,
)


class MaintenanceRequestSerializer(serializers.ModelSerializer):
    """Read shape. Everything a resident is allowed to see about their ticket."""

    category_display = serializers.CharField(source="get_category_display", read_only=True)
    priority_display = serializers.CharField(source="get_priority_display", read_only=True)
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    property_address = serializers.SerializerMethodField()

    class Meta:
        model = MaintenanceRequest
        fields = [
            "id", "title", "description",
            "category", "category_display",
            "priority", "priority_display",
            "status", "status_display",
            "photo_url", "preferred_access_time",
            "staff_notes", "resolved_at",
            "property_address",
            "created_at", "updated_at",
        ]
        # staff_notes is readable but never writable: it is the record of what
        # staff did, and a resident editing it would destroy the audit trail
        # the manual process depends on.
        read_only_fields = fields

    def get_property_address(self, obj) -> str | None:
        return str(obj.property) if obj.property_id else None


class MaintenanceRequestCreateSerializer(serializers.ModelSerializer):
    """
    Create shape. Four writable fields and nothing else.

    `status` is absent deliberately — it defaults to SUBMITTED in the model, and
    the only way it moves is through staff action.
    """

    category = serializers.ChoiceField(choices=MaintenanceCategory.choices)
    priority = serializers.ChoiceField(
        choices=MaintenancePriority.choices, default=MaintenancePriority.MEDIUM,
    )

    class Meta:
        model = MaintenanceRequest
        fields = [
            "title", "description", "category", "priority",
            "preferred_access_time", "photo_url",
        ]

    def validate_title(self, value: str) -> str:
        cleaned = value.strip()
        if len(cleaned) < 4:
            raise serializers.ValidationError(
                "Give the problem a short title so staff can tell tickets apart.",
            )
        return cleaned

    def validate_description(self, value: str) -> str:
        cleaned = value.strip()
        if len(cleaned) < 10:
            raise serializers.ValidationError(
                "Describe the problem in a sentence or two — what is wrong, and where.",
            )
        return cleaned


class ClientDocumentSerializer(serializers.ModelSerializer):
    document_type_display = serializers.CharField(source="get_document_type_display", read_only=True)
    expires_soon = serializers.SerializerMethodField()

    class Meta:
        model = ClientDocument
        fields = [
            "id", "name", "file_url",
            "document_type", "document_type_display",
            "is_signed", "created_at", "expires_at", "expires_soon",
        ]
        read_only_fields = fields

    def get_expires_soon(self, obj) -> bool:
        """
        Within 30 days, per the spec's expiry warning.

        Computed here rather than in the client so every consumer agrees on the
        threshold — a lease that the web portal calls "expiring" and a native
        app calls "fine" is a support call.
        """
        from datetime import timedelta

        from django.utils import timezone

        if not obj.expires_at:
            return False
        return obj.expires_at <= timezone.now() + timedelta(days=30)
