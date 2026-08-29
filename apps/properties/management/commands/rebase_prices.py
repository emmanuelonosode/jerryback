"""
Bring every stored price onto the current discount.

WHY THIS EXISTS SEPARATELY FROM THE SYNC. The sync only ever touches homes that
are in today's feed. Production carries records that predate the current
pricing rule, and records that have since left the feed but are still published
inside their grace window - and those were advertising a discount somewhere
between 15% and 18%, applied by an ad-hoc script nobody kept. Two homes on the
same street, one at 17% off and one at 20%, is not a rounding difference a
renter will read as anything other than a mistake.

SAFE TO RUN REPEATEDLY. `discounted_rent` always works off
`original_price_cents` - the partner's advertised rent, stored verbatim - so
this converges rather than compounding. Running it twice changes nothing the
second time, and `--dry-run` proves that before you commit.

BASE RENT ONLY. Fees are not touched. `total_monthly_cents` is computed as
rent plus required monthly fees, so the totals shown across the site follow
from this automatically.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand
from django.db.models import F

from apps.properties.ingest import DISCOUNT_BASIS_POINTS, discounted_rent
from apps.properties.models import Property


class Command(BaseCommand):
    help = "Recompute every advertised rent as original price less the standard discount."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Report without writing.")
        parser.add_argument(
            "--limit", type=int, default=0,
            help="Only process this many records. For a cautious first pass.",
        )

    def handle(self, *args, **options):
        dry = options["dry_run"]
        pct = DISCOUNT_BASIS_POINTS / 100

        queryset = Property.objects.exclude(original_price_cents=None).exclude(
            original_price_cents=0
        ).order_by("slug")
        if options["limit"]:
            queryset = queryset[: options["limit"]]

        changes: list[tuple[str, int, int, int]] = []
        for prop in queryset.only("id", "slug", "price_cents", "original_price_cents").iterator(
            chunk_size=1000
        ):
            target = discounted_rent(prop.original_price_cents)
            if target != prop.price_cents and target > 0:
                changes.append((prop.slug, prop.original_price_cents, prop.price_cents, target))

        if not changes:
            self.stdout.write(self.style.SUCCESS(f"Every price is already at {pct:g}% off. Nothing to do."))
            return

        # The biggest movers first: if something here is wrong, it is wrong at
        # the top of this list, and a person scanning ten lines will see it.
        by_size = sorted(changes, key=lambda c: abs(c[3] - c[2]), reverse=True)
        self.stdout.write(f"{len(changes)} of {queryset.count()} prices need re-basing to {pct:g}% off.")
        self.stdout.write("Largest movements:")
        for slug, original, was, now in by_size[:10]:
            delta = (now - was) / 100
            self.stdout.write(
                f"  {slug[:46]:<46} ${original/100:>8,.0f} orig | "
                f"${was/100:>7,.0f} -> ${now/100:>7,.0f}  ({delta:+,.0f})"
            )

        if dry:
            self.stdout.write(self.style.WARNING("Dry run - nothing written."))
            return

        # `update()` per record rather than `save()`: `save()` would rebuild
        # `search_text` and re-run slug uniqueness for a change that touches
        # neither, on several thousand rows, on a one-core box.
        written = 0
        for slug, _original, _was, now in changes:
            written += Property.objects.filter(slug=slug).update(price_cents=now)

        self.stdout.write(self.style.SUCCESS(f"Re-based {written} prices to {pct:g}% off the original."))
