"""
Documents staff ask an applicant or resident for, and the files they send back.

The round trip: staff add a request on the application in the admin (what they
need, and why, in words the person will read); the person is emailed a link to
their portal; they upload there; staff are alerted and accept it, or ask again
with a note saying what was wrong. Files are stored privately - see
apps/core/private_storage.py - and are only reachable through the admin.
"""

from django.conf import settings

from apps.core.private_storage import RejectedUpload, store_private
from apps.integrations.alerts import admin_link, deliver_in_background, describe, notify_staff
from apps.integrations.models import queue_email

from .models import ApplicationDocument, DocumentRequest, DocumentRequestStatus

SUBDIR = "application-docs"
#: A pay stub for each of two jobs plus two bank statements, with room to spare.
MAX_FILES_PER_UPLOAD = 6


def portal_documents_url() -> str:
    return f"{settings.PUBLIC_SITE_URL.rstrip('/')}/portal/documents"


def _first_name(application) -> str:
    return (application.first_name or "").strip() or "there"


def email_request(req: DocumentRequest) -> None:
    """Tell the person what is needed and where to send it."""
    app = req.application
    if not app.email:
        return
    lines = [
        f"Hi {_first_name(app)},",
        "",
        f"To keep your application for {app.property or 'your new home'} moving, please send us:",
        "",
        f"  {req.get_kind_display()}",
    ]
    if req.message:
        lines += ["", req.message]
    if req.due_at:
        lines += ["", f"If you can, please send it by {req.due_at:%A %d %B}."]
    lines += [
        "",
        "Upload it in your portal - a phone photo or a PDF is fine:",
        f"  {portal_documents_url()}",
        "",
        "Questions? Just reply to this email.",
    ]
    queued = queue_email(
        send_now=False, to_email=app.email,
        subject=f"We need one more thing: {req.get_kind_display().lower()}",
        body_text="\n".join(lines), template="document-request",
    )
    if queued is not None:
        deliver_in_background([queued])


def email_rejection(req: DocumentRequest) -> None:
    """Ask again, saying what was wrong with what they sent."""
    app = req.application
    if not app.email:
        return
    body = (
        f"Hi {_first_name(app)},\n\n"
        f"Thank you for sending your {req.get_kind_display().lower()}. We need another copy:\n\n"
        f"{req.review_note}\n\n"
        f"Upload it here:\n  {portal_documents_url()}\n"
    )
    queued = queue_email(
        send_now=False, to_email=app.email,
        subject=f"Please send your {req.get_kind_display().lower()} again",
        body_text=body, template="document-request",
    )
    if queued is not None:
        deliver_in_background([queued])


def accept_upload(req: DocumentRequest, files, *, user) -> list[ApplicationDocument]:
    """
    Store the files against the request and mark it submitted.

    All-or-nothing on validation: one wrong file type refuses the upload with
    the reason, rather than keeping half of what somebody meant to send.
    """
    if not files:
        raise RejectedUpload("Choose at least one file to upload.")
    if len(files) > MAX_FILES_PER_UPLOAD:
        raise RejectedUpload(f"Send up to {MAX_FILES_PER_UPLOAD} files at a time.")

    stored = []
    for upload in files:
        name = store_private(upload, subdir=SUBDIR, prefix=f"{req.application_id}-{req.kind.lower()}", what="document")
        stored.append((upload, name))

    docs = [
        ApplicationDocument.objects.create(
            application=req.application, request=req, kind=req.kind, stored_name=name,
            original_name=(getattr(upload, "name", "") or "")[:200],
            content_type=upload.content_type, size=upload.size, uploaded_by=user,
        )
        for upload, name in stored
    ]
    req.status = DocumentRequestStatus.SUBMITTED
    req.save(update_fields=["status", "updated_at"])

    app = req.application
    notify_staff(
        subject=f"Document received: {req.get_kind_display()} - {app.first_name} {app.last_name}".strip(),
        body=describe([
            ("Applicant", f"{app.first_name} {app.last_name}".strip()),
            ("Document", req.get_kind_display()),
            ("Files", ", ".join(d.original_name or d.stored_name for d in docs)),
            ("Open in admin", admin_link(f"crm/rentalapplication/{app.id}/change")),
        ]) + "\n\nReview it on the application's Documents tab: accept it, or ask again with a note.\n",
        kind="document",
    )
    return docs
