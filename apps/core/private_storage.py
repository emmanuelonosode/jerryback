"""
Files people send us that must never be publicly reachable.

NOT UNDER MEDIA_ROOT. `MEDIA_URL` is served by the web server, so a file
written there is reachable by anyone who knows or guesses its URL. IDs, pay
stubs and bank statements go in a sibling directory the web server does not
serve at all; staff reach them through an authenticated admin view.

THE EXTENSION IS NEVER TAKEN FROM THE UPLOAD. It is decided from the reported
MIME type against a fixed allowlist and the filename is generated, so an
upload cannot choose where it lands or what it is called - a filename under
our control can carry path separators, and a `.html` or `.svg` stored
somewhere served is stored cross-site scripting.

Extracted from the tour-ID upload so every private upload follows one set of
rules rather than a copy that drifts.
"""

import logging
import uuid
from pathlib import Path

from django.conf import settings

logger = logging.getLogger(__name__)

# Photos and scans. Nothing that a browser would execute.
EXTENSION_FOR = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/webp": "webp",
    "image/heic": "heic",
    "image/heif": "heic",
    "application/pdf": "pdf",
}

# A phone photo or a multi-page PDF statement is comfortably under this.
MAX_BYTES = 10 * 1024 * 1024


class RejectedUpload(Exception):
    """Carries the sentence shown to the person who tried to upload."""


def private_dir(subdir: str) -> Path:
    root = getattr(settings, "PRIVATE_UPLOAD_ROOT", None)
    base = Path(root) if root else Path(settings.MEDIA_ROOT).parent / "private-uploads"
    return Path(base) / subdir


def store_private(upload, *, subdir: str, prefix: str, what: str = "file") -> str:
    """
    Save one upload and return the generated filename.

    Raises `RejectedUpload` with a message meant for the person uploading -
    the two failures need different fixes, so they get different sentences.
    """
    extension = EXTENSION_FOR.get(getattr(upload, "content_type", ""))
    if not extension:
        raise RejectedUpload(
            f"That file type is not supported. A photo of your {what} (PNG, JPG or HEIC) "
            "or a PDF works."
        )
    if upload.size > MAX_BYTES:
        raise RejectedUpload(
            f"That file is {upload.size / 1024 / 1024:.1f}MB and the limit is 10MB. "
            "A normal phone photo is well under that."
        )

    directory = private_dir(subdir)
    directory.mkdir(parents=True, exist_ok=True)
    filename = f"{prefix}-{uuid.uuid4().hex[:12]}.{extension}"

    with open(directory / filename, "wb") as handle:
        for chunk in upload.chunks():
            handle.write(chunk)

    # 0600: readable by the application user and nobody else on the box.
    (directory / filename).chmod(0o600)
    return filename


def resolve_private(subdir: str, filename: str) -> Path | None:
    """The stored file's path, or None if it is missing or escapes `subdir`."""
    directory = private_dir(subdir)
    safe = Path((filename or "").rstrip("/")).name
    if not safe:
        return None
    path = directory / safe
    try:
        if not path.resolve().is_relative_to(directory.resolve()):
            return None
    except (OSError, ValueError):
        return None
    return path if path.is_file() else None


def delete_private(subdir: str, filename: str) -> bool:
    """Remove one stored file. Best effort - already gone is success."""
    try:
        # `.name` strips any directory component, so a value that somehow
        # acquired a path cannot reach outside the directory.
        (private_dir(subdir) / Path(filename).name).unlink(missing_ok=True)
        return True
    except OSError:
        logger.exception("could not delete private upload %s/%s", subdir, filename)
        return False
