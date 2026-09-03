"""
Remove homes that do not have enough photographs to be worth listing.

A single exterior shot tells a renter almost nothing, and a listing with no
photograph at all reads as a scam listing in this category - the opposite of
what this site exists to be. So anything under `MIN_IMAGES` comes out.

THIS CONVERGES TO ZERO, and that is the point. `sync_from_supabase` refuses to
import a feed row under the same threshold, so this is not a nightly fight
against the importer: it clears what is already stored and then finds nothing.
Without that guard the two jobs would delete and recreate the same few hundred
records every night, which Google sees as URLs appearing and disappearing.

DELETION IS PERMANENT AND THIS IS A CRON JOB, so it carries the same ceiling
the sync's retirement pass does. A rule that suddenly matches most of the
catalogue is a bug in the rule, not a very bad night for photography.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand
from django.db.models import Count

from apps.properties.management.commands.sync_from_supabase import MIN_IMAGES
from apps.properties.models import Property

# Past this share of the catalogue, stop and report rather than delete.
DELETE_CEILING = 0.20


class Command(BaseCommand):
    help = f"Withdraw properties with fewer than {MIN_IMAGES} photographs."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Report without changing anything.")
        parser.add_argument(
            "--force",
            action="store_true",
            help="Delete even when the count exceeds the safety ceiling.",
        )

    def handle(self, *args, **options):
        dry = options["dry_run"]

        stale = Property.objects.annotate(image_count=Count("images")).filter(
            image_count__lt=MIN_IMAGES
        )
        count = stale.count()
        total = Property.objects.count()

        if count == 0:
            self.stdout.write(
                self.style.SUCCESS(f"Every property has at least {MIN_IMAGES} photographs.")
            )
            return

        share = count / total if total else 0
        indexed = stale.filter(is_published=True).count()

        self.stdout.write(
            f"{count} of {total} properties have fewer than {MIN_IMAGES} photographs "
            f"({share:.1%}); {indexed} of them are published."
        )
        for slug in stale.values_list("slug", flat=True)[:10]:
            self.stdout.write(f"  {slug}")
        if count > 10:
            self.stdout.write(f"  … and {count - 10} more")

        if share > DELETE_CEILING and not options["force"]:
            self.stderr.write(self.style.ERROR(
                f"REFUSING TO DELETE {count} of {total} ({share:.0%}). That is past the "
                f"{DELETE_CEILING:.0%} ceiling and looks like the images failed to import "
                f"rather than a catalogue with no photographs. Nothing was deleted. "
                f"Re-run with --force if this is genuinely correct."
            ))
            return

        if dry:
            self.stdout.write(self.style.WARNING("Dry run - nothing changed."))
            return

        '''
        OFF THE SITE IMMEDIATELY; DELETED ONLY IF IT WAS NEVER LIVE.

        A listing without photographs should stop being offered the moment we
        notice, and unpublishing does that - it leaves `public()`, leaves the
        sitemap, and stops being served.

        Deleting the row as well is a different act. A row that has been live
        has a URL that may be in Google's index and in somebody's messages, and
        destroying the record is how a catalogue ends up scattering URLs across
        Search Console. Keeping it means that if the photographs come back the
        same record is republished at the same address, rather than a new row
        arriving and the old one having simply vanished.

        So: published records are withdrawn, and only records that were never
        published - imports that never should have happened - are removed.
        '''
        published = stale.filter(is_published=True)
        withdrawn = published.update(is_published=False, status="off-market")

        never_live = Property.objects.annotate(image_count=Count("images")).filter(
            image_count__lt=MIN_IMAGES, is_published=False, status="off-market"
        ).exclude(pk__in=published.values("pk"))
        deleted, details = never_live.delete()

        self.stdout.write(self.style.SUCCESS(
            f"Withdrawn {withdrawn} listings (row and URL kept), "
            f"deleted {deleted} that were never published: {details}"
        ))
