"""
Applicant-facing serializers.

AN APPLICATION IS THE MOST SENSITIVE RECORD THIS SYSTEM HOLDS. It carries a date
of birth, the last four of a social security number, a current address, and a
landlord's phone number. The serializer below is what an applicant sees of their
OWN application, and it still omits `ssn_last4` and `date_of_birth`: the
applicant already knows them, echoing them back adds nothing, and every surface
that renders them is another place they can leak — into a screenshot, a support
ticket, or a browser cache.
"""

from rest_framework import serializers

from apps.properties.models import Property

from .models import RentalApplication


class ApplicationPropertySerializer(serializers.ModelSerializer):
    bathrooms = serializers.FloatField(read_only=True)
    primary_image_url = serializers.SerializerMethodField()
    full_address = serializers.SerializerMethodField()

    class Meta:
        model = Property
        fields = [
            "id", "slug", "title", "address", "city", "state", "zip_code",
            "full_address", "bedrooms", "bathrooms", "sqft",
            "price_cents", "price_label", "primary_image_url",
        ]
        read_only_fields = fields

    def get_primary_image_url(self, obj) -> str | None:
        # PropertyImage orders primary-first, so the first row is the hero shot.
        image = obj.images.first()
        if image is None:
            return None
        from apps.properties.serializers import absolute_media_url

        return absolute_media_url(image.url, self.context.get("request"))

    def get_full_address(self, obj) -> str:
        return f"{obj.address}, {obj.city}, {obj.state} {obj.zip_code}".strip()


class MyApplicationSerializer(serializers.ModelSerializer):
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    property = ApplicationPropertySerializer(read_only=True)
    move_in = serializers.SerializerMethodField()
    assigned_agent = serializers.SerializerMethodField()

    class Meta:
        model = RentalApplication
        fields = [
            "id", "status", "status_display",
            "property", "move_in_date",
            "application_fee_cents", "is_fee_paid",
            "months_rent_upfront", "security_deposit_cents",
            "lease_admin_fee_cents", "pet_fee_cents",
            "move_in", "assigned_agent",
            "created_at",
        ]
        read_only_fields = fields

    def get_move_in(self, obj) -> dict | None:
        """
        The itemised move-in total the dashboard leads with.

        Delegated to the existing calculator rather than summed here, so the
        figure an applicant sees on the portal is the same one the rest of the
        system computes. Two implementations of "what you owe on day one" is how
        a portal and an invoice end up disagreeing in front of a resident.
        """
        from .move_in import calculate_move_in

        if obj.property_id is None or not obj.property.price_cents:
            return None

        breakdown = calculate_move_in(
            monthly_rent_cents=obj.property.price_cents,
            months_upfront=obj.months_rent_upfront or 1,
            security_deposit_cents=obj.security_deposit_cents,
            application_fee_cents=obj.application_fee_cents or 0,
            lease_admin_fee_cents=obj.lease_admin_fee_cents,
            pet_fee_cents=obj.pet_fee_cents,
        )
        return {
            "line_items": breakdown.line_items,
            "total_cents": breakdown.total_cents,
            "warnings": breakdown.warnings,
        }

    def get_assigned_agent(self, obj) -> dict | None:
        """
        Who to contact. A name and a work email only.

        The brief's whole position is that a renter can reach a named human, so
        this is deliberately present — but it is the agent's work contact, not
        their profile, and nothing here is writable.
        """
        client = getattr(obj.user, "client", None) if obj.user_id else None
        agent = getattr(client, "preferred_agent", None) if client else None
        if agent is None:
            return None
        return {
            "name": f"{agent.first_name} {agent.last_name}".strip(),
            "email": agent.email,
            "phone": agent.phone or "",
        }
