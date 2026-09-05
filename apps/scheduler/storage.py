"""
Where a tour applicant's ID lives while we need it.

NOT UNDER MEDIA_ROOT. `MEDIA_URL` is served by the web server, so a file
written there is reachable by anyone who knows or guesses its URL. A driver's
licence is the single most sensitive thing this site ever receives, and a
random filename is obfuscation, not access control. These go in a sibling
directory the web server does not serve at all, so reaching one requires shell
access to the box rather than a lucky URL.

THE EXTENSION IS NEVER TAKEN FROM THE UPLOAD. It is decided from the reported
MIME type against a fixed allowlist and the filename is generated, so an
upload cannot choose where it lands or what it is called. The same reasoning
as the payment-proof upload in the frontend, for the same reason: a filename
under our control can carry path separators, and a `.html` or `.svg` stored
somewhere served is stored cross-site scripting.

THEY ARE MEANT TO BE DELETED. `TourRequest.purge_ids` removes them once the
request has been reviewed, and `purge_tour_ids` runs that on a schedule. An ID
we still hold a week after the viewing is a liability with no purpose.
"""

import logging
import uuid
from pathlib import Path

from django.conf import settings

logger = logging.getLogger(__name__)

# A photo of a card, or a scan of one. Nothing else is an ID.
EXTENSION_FOR = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/webp": "webp",
    "image/heic": "heic",
    "image/heif": "heic",
    "application/pdf": "pdf",
}

# A phone photo of a licence is comfortably under this; a 15MB one needs
# resizing rather than a bigger limit.
MAX_BYTES = 10 * 1024 * 1024


def tour_id_dir() -> Path:
    root = getattr(settings, "PRIVATE_UPLOAD_ROOT", None)
    base = Path(root) if root else Path(settings.MEDIA_ROOT).parent / "private-uploads"
    return Path(base) / "tour-ids"


class RejectedUpload(Exception):
    """Carries the sentence shown to the person who tried to upload."""


def store_tour_id(upload, *, tour_public_id, side: str) -> str:
    """
    Save one ID image and return the generated filename.

    Raises `RejectedUpload` with a message meant to be read by the applicant -
    the two failures need different fixes, so they get different sentences.
    """
    extension = EXTENSION_FOR.get(getattr(upload, "content_type", ""))
    if not extension:
        raise RejectedUpload(
            "That file type is not supported. A photo of your ID (PNG, JPG or HEIC) "
            "or a PDF scan works."
        )
    if upload.size > MAX_BYTES:
        raise RejectedUpload(
            f"That file is {upload.size / 1024 / 1024:.1f}MB and the limit is 10MB. "
            "A normal phone photo is well under that."
        )

    directory = tour_id_dir()
    directory.mkdir(parents=True, exist_ok=True)
    filename = f"{tour_public_id}-{side}-{uuid.uuid4().hex[:8]}.{extension}"

    with open(directory / filename, "wb") as handle:
        for chunk in upload.chunks():
            handle.write(chunk)

    # 0600: readable by the application user and nobody else on the box.
    (directory / filename).chmod(0o600)
    return filename


def delete_tour_id(filename: str) -> bool:
    """Remove one stored ID. Best effort - already gone is success."""
    try:
        # `.name` strips any directory component, so a value that somehow
        # acquired a path cannot reach outside the directory.
        (tour_id_dir() / Path(filename).name).unlink(missing_ok=True)
        return True
    except OSError:
        logger.exception("could not delete tour ID %s", filename)
        return False
