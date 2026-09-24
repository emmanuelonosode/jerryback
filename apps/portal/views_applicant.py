"""
Portal endpoints tied to the caller's rental application: documents staff have
asked for, and the optional guarantor.

Ownership is resolved from the signed-in user and nothing else, as everywhere
in the portal. An application started before the account existed is linked by
email first - the same rule `my_applications` applies - so a person who applied
as a guest and then made an account sees their own requests.
"""

from django.db.models import Q
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes, throttle_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle

from apps.core.money import format_usd
from apps.core.private_storage import RejectedUpload
from apps.crm.documents import accept_upload
from apps.crm.models import (
    ApplicationStatus, DocumentRequest, DocumentRequestStatus, Guarantor, RentalApplication,
)


class UploadThrottle(ScopedRateThrottle):
    scope = "upload"


def _my_applications(user):
    """
    The caller's applications, including guest ones under their email.

    Read-only on purpose: linking a guest application to the account is done
    once, by `my_applications`. Doing it here as well meant three concurrent
    writes on every load of the documents page, for rows already linked.
    """
    own = Q(user=user)
    if user.email:
        own |= Q(user__isnull=True, email__iexact=user.email.strip())
    return RentalApplication.objects.filter(own)


def _request_payload(req: DocumentRequest) -> dict:
    return {
        "id": str(req.id),
        "kind": req.kind,
        "kind_label": req.get_kind_display(),
        "message": req.message,
        "due_at": req.due_at.isoformat() if req.due_at else None,
        "status": req.status,
        "status_label": req.get_status_display(),
        "review_note": req.review_note if req.status == DocumentRequestStatus.REJECTED else "",
        "home": str(req.application.property) if req.application.property_id else None,
        "created_at": req.created_at.isoformat(),
        "files": [
            {"name": d.original_name or "Uploaded file", "uploaded_at": d.created_at.isoformat()}
            for d in req.documents.all()
        ],
        # Accepted is final; anything else can take another file.
        "can_upload": req.status != DocumentRequestStatus.ACCEPTED,
    }


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def document_requests(request):
    requests = (
        DocumentRequest.objects.filter(application__in=_my_applications(request.user))
        .select_related("application__property")
        .prefetch_related("documents")
    )
    return Response([_request_payload(r) for r in requests])


@api_view(["POST"])
@permission_classes([IsAuthenticated])
@throttle_classes([UploadThrottle])
def upload_for_request(request, request_id):
    req = (
        DocumentRequest.objects.filter(id=request_id, application__in=_my_applications(request.user))
        .select_related("application__property")
        .first()
    )
    if req is None:
        return Response({"detail": "No such request."}, status=status.HTTP_404_NOT_FOUND)
    if req.status == DocumentRequestStatus.ACCEPTED:
        return Response({"detail": "We already have this one - thank you."}, status=status.HTTP_409_CONFLICT)

    files = request.FILES.getlist("files") or request.FILES.getlist("file")
    try:
        accept_upload(req, files, user=request.user)
    except RejectedUpload as refusal:
        return Response({"detail": str(refusal)}, status=status.HTTP_400_BAD_REQUEST)
    req.refresh_from_db()
    return Response(_request_payload(req), status=status.HTTP_201_CREATED)


def _guarantor_payload(g: Guarantor | None) -> dict | None:
    if g is None:
        return None
    return {
        "fullName": g.full_name,
        "relationship": g.relationship,
        "email": g.email,
        "phone": g.phone,
        "monthlyIncomeCents": g.monthly_income_cents,
        "monthlyIncome": format_usd(g.monthly_income_cents) if g.monthly_income_cents is not None else None,
    }


@api_view(["GET", "PUT"])
@permission_classes([IsAuthenticated])
def guarantor(request):
    """The optional guarantor on the caller's most recent submitted application."""
    app = (
        _my_applications(request.user).exclude(status=ApplicationStatus.DRAFT)
        .order_by("-created_at").first()
    )
    if app is None:
        return Response({"application": None, "guarantor": None, "editable": False})

    # Once the lease is signed the parties are fixed; changing them is a
    # conversation with staff, not a form.
    editable = app.lease_signed_at is None
    if request.method == "GET":
        return Response({
            "application": str(app.id),
            "guarantor": _guarantor_payload(Guarantor.objects.filter(application=app).first()),
            "editable": editable,
        })

    if not editable:
        return Response(
            {"detail": "Your lease is signed, so the guarantor can only be changed by our team. Reply to any of our emails."},
            status=status.HTTP_409_CONFLICT,
        )

    data = request.data or {}
    name = str(data.get("fullName") or "").strip()
    if not name:
        Guarantor.objects.filter(application=app).delete()
        return Response({"application": str(app.id), "guarantor": None, "editable": True})

    income = data.get("monthlyIncomeCents")
    if income is not None and (not isinstance(income, int) or income < 0):
        return Response({"detail": "Enter the guarantor's monthly income as a number."}, status=status.HTTP_400_BAD_REQUEST)
    g, _ = Guarantor.objects.update_or_create(
        application=app,
        defaults={
            "full_name": name[:200],
            "relationship": str(data.get("relationship") or "").strip()[:100],
            "email": str(data.get("email") or "").strip()[:254],
            "phone": str(data.get("phone") or "").strip()[:20],
            "monthly_income_cents": income,
        },
    )
    return Response({"application": str(app.id), "guarantor": _guarantor_payload(g), "editable": True})
