import mimetypes
from pathlib import Path
from django.http import FileResponse, Http404
from django.contrib.admin.views.decorators import staff_member_required
from .storage import tour_id_dir

@staff_member_required
def secure_tour_id_view(request, filename):
    directory = tour_id_dir()
    safe_name = Path(filename.rstrip("/")).name
    if not safe_name:
        raise Http404("Invalid filename")
    filepath = directory / safe_name
    
    # Ensure the path is within the directory (prevent directory traversal)
    try:
        if not filepath.resolve().is_relative_to(directory.resolve()):
            raise Http404("Invalid file path")
    except ValueError:
        raise Http404("Invalid file path")
        
    if not filepath.exists() or not filepath.is_file():
        raise Http404("File not found")
        
    content_type, _ = mimetypes.guess_type(str(filepath))
    content_type = content_type or "application/octet-stream"

    if request.GET.get("download") == "1":
        return FileResponse(open(filepath, "rb"), as_attachment=True, filename=safe_name, content_type=content_type)

    response = FileResponse(open(filepath, "rb"), content_type=content_type)
    response["Content-Disposition"] = f'inline; filename="{safe_name}"'
    return response
