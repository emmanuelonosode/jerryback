from django.core.management.base import BaseCommand

from apps.properties.models import RENT_RESTATEMENT, PropertyFee


class Command(BaseCommand):
    """
    Delete fee rows that only restate the rent.

    The API already excludes them, so the site is correct without this - but
    they remain visible in the admin, where a member of staff reading a home's
    fee list sees the rent listed twice and has no way to know which one counts.

    Re-importable: these rows come from the partner feed, so anything deleted
    here comes back on the next sync unless the importer is fixed too. Run with
    --dry-run first.
    """

    help = "Remove partner-feed fee rows that duplicate the property's base rent."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would be deleted without deleting it.",
        )

    def handle(self, *args, **options):
        rows = PropertyFee.objects.filter(RENT_RESTATEMENT)
        count = rows.count()

        if options["dry_run"]:
            for fee in rows.select_related("property")[:10]:
                self.stdout.write(
                    f"  would delete {fee.label!r} {fee.amount_cents} "
                    f"on {fee.property.address}"
                )
            self.stdout.write(self.style.WARNING(f"{count} row(s) would be deleted."))
            return

        deleted, _ = rows.delete()
        self.stdout.write(self.style.SUCCESS(f"Deleted {deleted} rent-restatement fee row(s)."))
