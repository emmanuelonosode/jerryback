import mimetypes
from pathlib import Path
from django.http import FileResponse, Http404
from django.contrib.admin.views.decorators import staff_member_required
from django.conf import settings

@staff_member_required
def secure_payment_proof_view(request, filename):
    safe_name = Path(filename).name
    if not safe_name or safe_name != filename:
        raise Http404("Invalid filename")

    # Search possible storage locations
    candidates = [
        Path(settings.BASE_DIR) / "private-uploads" / "proofs" / safe_name,
        Path("/home/frontend/htdocs/skeltonrealtygroup.com/private-uploads/proofs") / safe_name,
        Path(settings.MEDIA_ROOT) / safe_name,
        Path(settings.MEDIA_ROOT) / "proofs" / safe_name,
    ]
    
    filepath = None
    for candidate in candidates:
        try:
            if candidate.exists() and candidate.is_file():
                filepath = candidate
                break
        except Exception:
            continue

    if not filepath:
        raise Http404("Payment proof not found")

    content_type, _ = mimetypes.guess_type(str(filepath))
    content_type = content_type or "application/octet-stream"

    if request.GET.get("download") == "1":
        return FileResponse(open(filepath, "rb"), as_attachment=True, filename=safe_name, content_type=content_type)

    response = FileResponse(open(filepath, "rb"), content_type=content_type)
    response["Content-Disposition"] = f'inline; filename="{safe_name}"'
    return response
