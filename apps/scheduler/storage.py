"""
Where a tour applicant's ID lives while we need it.

The rules - private directory, generated filename, MIME allowlist, size cap -
live in `apps/core/private_storage.py`, shared with application documents.

THEY ARE MEANT TO BE DELETED. `TourRequest.purge_ids` removes them once the
request has been reviewed, and `purge_tour_ids` runs that on a schedule. An ID
we still hold a week after the viewing is a liability with no purpose.
"""

from pathlib import Path

from apps.core.private_storage import (  # noqa: F401 - re-exported for callers
    EXTENSION_FOR,
    MAX_BYTES,
    RejectedUpload,
    delete_private,
    private_dir,
    store_private,
)

SUBDIR = "tour-ids"


def tour_id_dir() -> Path:
    return private_dir(SUBDIR)


def store_tour_id(upload, *, tour_public_id, side: str) -> str:
    """Save one ID image and return the generated filename."""
    return store_private(upload, subdir=SUBDIR, prefix=f"{tour_public_id}-{side}", what="ID")


def delete_tour_id(filename: str) -> bool:
    """Remove one stored ID. Best effort - already gone is success."""
    return delete_private(SUBDIR, filename)
