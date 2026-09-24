"""
Staff viewer for documents applicants uploaded.

Admin session only, and additionally the `application:read` grant: being staff
is not the same as being allowed to read an applicant's bank statement - an
accountant role, say, has no business opening a pay stub. See
apps/accounts/permissions.py for why roles are grants rather than a rank.
"""

import mimetypes

from django.contrib.admin.views.decorators import staff_member_required
from django.http import FileResponse, Http404

from apps.accounts.permissions import APPLICATION_READ, can
from apps.core.private_storage import resolve_private

from .documents import SUBDIR
from .models import ApplicationDocument


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
