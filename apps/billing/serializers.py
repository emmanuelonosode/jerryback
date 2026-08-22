"""
Billing serializers for the resident portal.

BANK DETAILS ARE NOT PUBLIC DATA. `PaymentMethodConfigSerializer` carries an
account number and a routing number, and the model's own docstring is explicit
that these rails are the ones rental fraud runs on and that details are
published "only on the site, behind an application the applicant started". The
view that uses this serializer requires authentication for that reason, and
inactive methods are never serialised at all — a method that is not live has
either been switched off deliberately or has no details to give.
"""

from rest_framework import serializers

from .models import Invoice, Payment, PaymentMethodConfig, PaymentMethodKind


class PaymentMethodConfigSerializer(serializers.ModelSerializer):
    method_display = serializers.CharField(source="get_method_display", read_only=True)

    class Meta:
        model = PaymentMethodConfig
        fields = [
            "id", "method", "method_display", "display_name", "handle",
            "extra_instructions", "irreversible", "clearing_time",
            "recipient_name", "bank_name", "account_type",
            "account_number", "routing_number",
        ]
        read_only_fields = fields


class InvoiceSerializer(serializers.ModelSerializer):
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    received_cents = serializers.IntegerField(read_only=True)
    balance_cents = serializers.SerializerMethodField()

    class Meta:
        model = Invoice
        fields = [
            "id", "invoice_number", "title", "description",
            "issued_date", "due_date",
            "line_items", "subtotal_cents", "tax_basis_points", "tax_amount_cents",
            "total_cents", "received_cents", "balance_cents",
            "pdf_url", "status", "status_display",
            "created_at",
        ]
        read_only_fields = fields

    def get_balance_cents(self, obj) -> int:
        """
        What is still owed, floored at zero.

        An overpayment is a credit to be handled deliberately, not a negative
        balance rendered as "-$40.00 due", which reads as the resident being
        owed rent.
        """
        return max(0, obj.total_cents - obj.received_cents)


class PaymentSerializer(serializers.ModelSerializer):
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    method_display = serializers.CharField(source="get_payment_method_display", read_only=True)
    invoice_number = serializers.CharField(source="invoice.invoice_number", read_only=True, default=None)

    class Meta:
        model = Payment
        fields = [
            "id", "invoice", "invoice_number", "amount_cents",
            "payment_method", "method_display",
            "status", "status_display",
            "reference_id", "proof_image_url",
            "rejection_reason", "paid_at", "created_at",
        ]
        read_only_fields = fields


class SubmitProofSerializer(serializers.Serializer):
    """
    A resident telling us they have sent money on a manual rail.

    This records a CLAIM, not a receipt. Nothing here marks an invoice paid —
    the payment lands as PENDING_VERIFICATION and a person checks it against the
    bank. That gap is the entire point of the manual model: the alternative is a
    stranger being able to mark their own rent paid by typing a reference number.
    """

    invoice = serializers.UUIDField()
    amount_cents = serializers.IntegerField(min_value=1)
    payment_method = serializers.ChoiceField(choices=PaymentMethodKind.choices)
    reference_id = serializers.CharField(max_length=60, allow_blank=True, required=False, default="")
    proof_image_url = serializers.CharField(max_length=500, allow_blank=True, required=False, default="")

    def validate(self, attrs):
        if not attrs.get("reference_id", "").strip() and not attrs.get("proof_image_url", "").strip():
            raise serializers.ValidationError(
                "Add the reference from your transfer, or a screenshot of it, so staff can match "
                "the payment to your account.",
            )
        return attrs

    def validate_payment_method(self, value):
        active = PaymentMethodConfig.objects.filter(method=value, is_active=True).first()
        if active is None or not active.is_payable:
            raise serializers.ValidationError("That payment method is not currently accepted.")
        return value
