"""
Resident billing endpoints: invoices, payment history, payment rails, proof.

SCOPED TO request.user THROUGHOUT. Invoices carry a nullable `user`, so a filter
of `user=request.user` is not merely a convenience — writing it as
`Invoice.objects.all()` and trusting an id from the caller would hand every
resident the rent, fees and payment history of every other one.

WHY payment-config REQUIRES AUTHENTICATION, when the specification calls it
public: it returns an account number and a routing number. The model docstring
records the decision it belongs to — these rails are irreversible, they are the
ones rental scams use, and the mitigation is that details appear only to someone
who already has an application in progress. A public endpoint serving bank
details is the exact artefact a scammer copies to make a fake listing credible.
"""

from django.db.models import Q, Sum
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response

from .models import Invoice, InvoiceStatus, Payment, PaymentMethodConfig, PaymentStatus
from .serializers import (
    InvoiceSerializer,
    PaymentMethodConfigSerializer,
    PaymentSerializer,
    SubmitProofSerializer,
)


def _visible_invoices(user):
    """
    A resident's invoices: theirs directly, or attached to their application.

    DRAFT is excluded. A draft is staff working out what to charge; showing it
    would tell someone they owe a number that has not been agreed yet.
    """
    return (
        Invoice.objects.filter(Q(user=user) | Q(rental_application__user=user))
        .exclude(status=InvoiceStatus.DRAFT)
        .distinct()
    )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def my_invoices(request):
    return Response(InvoiceSerializer(_visible_invoices(request.user), many=True).data)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def my_payments(request):
    payments = (
        Payment.objects.filter(
            Q(invoice__user=request.user) | Q(rental_application__user=request.user),
        )
        .select_related("invoice")
        .distinct()
        .order_by("-created_at")
    )
    return Response(PaymentSerializer(payments, many=True).data)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def my_billing_summary(request):
    """
    The three figures the payments page leads with.

    Computed in one place because a "total paid" the dashboard and the payments
    page disagree about is worse than not showing it at all.
    """
    invoices = _visible_invoices(request.user)

    verified = Payment.objects.filter(
        Q(invoice__user=request.user) | Q(rental_application__user=request.user),
        status=PaymentStatus.VERIFIED,
    ).distinct()

    total_paid = verified.aggregate(total=Sum("amount_cents"))["total"] or 0
    outstanding = sum(
        max(0, inv.total_cents - inv.received_cents)
        for inv in invoices.filter(status=InvoiceStatus.SENT)
    )
    last = verified.order_by("-paid_at", "-created_at").first()

    return Response({
        "total_paid_cents": total_paid,
        "open_balance_cents": outstanding,
        "last_payment": PaymentSerializer(last).data if last else None,
    })


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def payment_config(request):
    methods = PaymentMethodConfig.objects.filter(is_active=True).order_by("display_name")
    # `is_payable` also excludes an active row whose details were emptied after
    # activation — the constraint stops that being saved, but a serializer that
    # trusts `is_active` alone would render a blank account number if it ever were.
    payable = [m for m in methods if m.is_payable]
    return Response(PaymentMethodConfigSerializer(payable, many=True).data)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def submit_proof(request):
    serializer = SubmitProofSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    data = serializer.validated_data

    # The invoice is re-fetched through the caller's own scope. Passing an id
    # that belongs to someone else must not attach a payment to their account.
    invoice = _visible_invoices(request.user).filter(id=data["invoice"]).first()
    if invoice is None:
        return Response(
            {"invoice": "No invoice of yours matches that id."},
            status=status.HTTP_404_NOT_FOUND,
        )

    if invoice.status == InvoiceStatus.VOID:
        return Response(
            {"invoice": "That invoice has been voided. Contact us before sending money."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    payment = Payment.objects.create(
        invoice=invoice,
        rental_application=invoice.rental_application,
        amount_cents=data["amount_cents"],
        payment_method=data["payment_method"],
        reference_id=data.get("reference_id", "").strip(),
        proof_image_url=data.get("proof_image_url", "").strip(),
        status=PaymentStatus.PENDING_VERIFICATION,
    )

    return Response(PaymentSerializer(payment).data, status=status.HTTP_201_CREATED)

@api_view(["GET"])
@permission_classes([AllowAny])
def payment_config_status(request):
    """
    How many rails are live — a count, and nothing else.

    The launch gate has to answer "can anyone actually pay yet?" without a
    resident's credentials, and `payment_config` rightly refuses to hand
    account handles to an anonymous caller. A bare count leaks nothing a
    fraudster could use while making the deploy check decisive instead of
    reporting "could not verify" forever.
    """
    active = [m for m in PaymentMethodConfig.objects.filter(is_active=True) if m.is_payable]
    return Response({"active": len(active)})
