"""
Copy partner-hosted photography onto storage we control.

WHY THIS EXISTS. Section 9 of the brief: images must be served from
infrastructure the company controls, never hotlinked from a partner's CDN.
Hotlinking makes the entire catalogue depend on a third party's uptime, hotlink
policy and URL stability — one change there and every listing on the site loses
its photograph at once, with no warning and nothing to roll back to.

`PropertyImage` already models the distinction: `url` is what renders, and it is
ours; `source_url` records where a file came from so a re-ingest is possible.
Nothing had ever populated it. This command is what moves a row from the second
column to the first.

    python manage.py ingest_images            # everything not yet ingested
    python manage.py ingest_images --force    # re-fetch even if already local
    python manage.py ingest_images --dry-run

WHAT IT DELIBERATELY DOES NOT DO. It does not resize, re-encode or strip
metadata. That belongs in the rendition pipeline, which has its own sizes to
emit and its own tests; doing it here would mean two places decide what a
listing photo looks like. This command has one job: stop depending on someone
else's server.

RIGHTS ARE NOT A TECHNICAL QUESTION. Copying a file does not create a licence to
publish it. The brief requires documented rights for every image, and the feed
agreement with the portfolio owner is where that lives. This command records
provenance in `source_url` so the question can always be answered; it does not
answer it.
"""

from __future__ import annotations

import hashlib
import mimetypes
import urllib.error
import urllib.request
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

from apps.properties.models import PropertyImage

# A partner CDN will happily serve a browser and refuse a bare urllib default,
# and an unattributed scraper-looking agent is bad manners besides.
USER_AGENT = "SkeltonRealtyGroup-ingest/1.0 (+https://skeltonrealtygroup.com)"

TIMEOUT_SECONDS = 30
MAX_BYTES = 20 * 1024 * 1024

# Only formats a browser will render as an <img>. An unexpected content type
# means the CDN returned an error page or a redirect to one, and writing that to
# disk with a .jpg extension produces a file that fails silently at render time.
ALLOWED_TYPES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/avif": ".avif",
}


class Command(BaseCommand):
    help = "Download partner-hosted listing photography onto local storage."

    def add_arguments(self, parser):
        parser.add_argument("--force", action="store_true",
                            help="Re-fetch images that already have a local url.")
        parser.add_argument("--dry-run", action="store_true",
                            help="Report what would be fetched; write nothing.")

    def handle(self, *args, **options):
        force: bool = options["force"]
        dry_run: bool = options["dry_run"]

        media_root = Path(settings.MEDIA_ROOT)
        target_dir = media_root / "listings"

        rows = list(PropertyImage.objects.select_related("property").order_by("property_id", "sort_order"))

        fetched = skipped = failed = 0

        for image in rows:
            remote = image.source_url or (image.url if image.url.startswith("http") else "")

            if not remote:
                # Already local and nothing recorded to re-fetch from.
                skipped += 1
                continue

            if not image.url.startswith("http") and not force:
                skipped += 1
                continue

            if dry_run:
                self.stdout.write(f"would fetch  {image.property.slug}  {remote[-60:]}")
                fetched += 1
                continue

            try:
                body, extension = self._download(remote)
            except Exception as error:  # noqa: BLE001 — one bad row must not stop the run
                self.stderr.write(self.style.ERROR(f"FAILED {image.property.slug}: {error}"))
                failed += 1
                continue

            # Content-addressed: the same bytes always land on the same path, so
            # re-running is idempotent and a changed photo cannot be served from
            # a stale cache under its old URL.
            digest = hashlib.sha256(body).hexdigest()[:16]
            name = f"{image.property.slug}-{digest}{extension}"

            target_dir.mkdir(parents=True, exist_ok=True)
            (target_dir / name).write_bytes(body)

            # source_url is only written if it was empty — the point of the
            # column is to remember the ORIGINAL provenance, and overwriting it
            # on a re-ingest would lose exactly that.
            if not image.source_url:
                image.source_url = remote
            image.url = f"{settings.MEDIA_URL}listings/{name}"
            image.save(update_fields=["url", "source_url"])

            fetched += 1
            self.stdout.write(f"  {image.property.slug}  →  {image.url}  ({len(body) // 1024} KB)")

        verb = "would fetch" if dry_run else "fetched"
        self.stdout.write(self.style.SUCCESS(
            f"\n{verb} {fetched} · skipped {skipped} · failed {failed}",
        ))

        if failed and not dry_run:
            self.stdout.write(
                "Rows that failed still point at the partner CDN. They render today and "
                "will break whenever that CDN changes — re-run rather than leaving them.",
            )

    def _download(self, url: str) -> tuple[bytes, str]:
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})

        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:  # noqa: S310
            content_type = (response.headers.get("Content-Type") or "").split(";")[0].strip().lower()

            if content_type not in ALLOWED_TYPES:
                raise ValueError(f"unexpected content type {content_type!r}")

            declared = response.headers.get("Content-Length")
            if declared and int(declared) > MAX_BYTES:
                raise ValueError(f"{int(declared) // 1024} KB exceeds the {MAX_BYTES // 1024} KB ceiling")

            # Read one byte past the ceiling so a response with no Content-Length
            # cannot stream unbounded into memory.
            body = response.read(MAX_BYTES + 1)

        if len(body) > MAX_BYTES:
            raise ValueError("response exceeded the size ceiling")
        if not body:
            raise ValueError("empty response")

        extension = ALLOWED_TYPES.get(content_type) or mimetypes.guess_extension(content_type) or ".jpg"
        return body, extension
