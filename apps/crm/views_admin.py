"""
Staff viewer for documents applicants uploaded.

Admin session only, and additionally the `application:read` grant: being staff
is not the same as being allowed to read an applicant's bank statement - an
accountant role, say, has no business opening a pay stub. See
apps/accounts/permissions.py for why roles are grants rather than a rank.
"""

import mimetypes

from django.contrib.admin.models import CHANGE, LogEntry
from django.contrib.admin.views.decorators import staff_member_required
from django.contrib.contenttypes.models import ContentType
from django.http import FileResponse, Http404
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone

from apps.accounts.permissions import APPLICATION_READ, APPLICATION_READ_PII, can
from apps.core.private_storage import resolve_private

from .documents import SUBDIR
from .models import ApplicationDocument, DocumentKind, RentalApplication


@staff_member_required
def secure_application_document(request, document_id):
    if not (request.user.is_superuser or can(getattr(request.user, "role", ""), APPLICATION_READ)):
        raise Http404("Not found")
    doc = ApplicationDocument.objects.filter(id=document_id).first()
    path = resolve_private(SUBDIR, doc.stored_name) if doc else None
    if path is None:
        raise Http404("Not found")

    content_type = doc.content_type or mimetypes.guess_type(str(path))[0] or "application/octet-stream"
    download = request.GET.get("download") == "1"
    response = FileResponse(
        open(path, "rb"), content_type=content_type, as_attachment=download,
        filename=doc.original_name or path.name,
    )
    # Never cached by a proxy or a shared browser profile.
    response["Cache-Control"] = "no-store, private"
    return response


def can_see_pii(user) -> bool:
    return user.is_superuser or can(getattr(user, "role", ""), APPLICATION_READ_PII)


@staff_member_required
def reveal_identity(request, application_id):
    """
    The full SSN/ITIN, licence number and date of birth, to check against an ID.

    Needs the `application:read-pii` grant (Admin role, or a superuser) - the
    same rule as before, now on a page of its own so the numbers are shown only
    when someone asks, and every look is logged to the application's History.
    """
    if not can_see_pii(request.user):
        raise Http404("Not found")
    app = RentalApplication.objects.filter(id=application_id).first()
    if app is None:
        raise Http404("Not found")

    LogEntry.objects.log_action(
        user_id=request.user.pk,
        content_type_id=ContentType.objects.get_for_model(RentalApplication).pk,
        object_id=str(app.pk),
        object_repr=str(app)[:200],
        action_flag=CHANGE,
        change_message="Viewed full identity numbers (SSN/ITIN, licence, date of birth).",
    )

    name = f"{app.first_name} {app.last_name}".strip() or app.email
    id_docs = app.documents.filter(kind=DocumentKind.ID).order_by("-created_at")
    from django.contrib import admin

    # The admin's own context (theme, sidebar, stylesheets), so this reads as
    # part of the admin rather than a bare page.
    return render(request, "admin/crm/reveal_identity.html", {
        **admin.site.each_context(request),
        "title": f"Identity check: {name}",
        "name": name,
        "full_name": " ".join(x for x in (app.first_name, app.middle_name, app.last_name) if x),
        "id_type": app.id_type,
        "ssn": app.ssn,
        "dob": app.date_of_birth.strftime("%B %d, %Y") if app.date_of_birth else "",
        "licence": app.drivers_license_number,
        "licence_state": app.drivers_license_state,
        "back_url": reverse("admin:crm_rentalapplication_change", args=[app.pk]),
        "id_documents": [
            {
                "url": reverse("secure-application-document", args=[d.id]),
                "name": d.original_name or d.stored_name,
                "when": timezone.localtime(d.created_at).strftime("%d %b %Y"),
            }
            for d in id_docs
        ],
    })
