"""
Re-run the draft-to-columns sync over every application.

WHY THIS EXISTS. The application form and the column sync disagreed about field
names - `preferredMoveInDate` vs `moveInDate`, `hasEviction` vs
`hasPriorEviction`, a household total vs a list of sources - so the move-in
date, income, background answers and household columns were never filled, and
the admin had nothing to show. The sync now reads the form's real names; this
applies it to applications saved before the fix. Safe to run more than once:
it only ever writes what the saved answers already say.
"""

from django.core.management.base import BaseCommand

from apps.crm.models import RentalApplication
from apps.crm.views import apply_draft_data


class Command(BaseCommand):
    help = "Backfill application columns and guarantors from saved draft answers."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        changed = 0
        for application in RentalApplication.objects.all().iterator():
            before = {f.attname: getattr(application, f.attname) for f in application._meta.concrete_fields}
            fields = apply_draft_data(application, dict(application.draft_data or {}))
            real = [f for f in fields if getattr(application, f) != before.get(f)]
            if not real:
                continue
            changed += 1
            if options["dry_run"]:
                self.stdout.write(f"  would update {application.id}: {', '.join(real)}")
            else:
                application.save(update_fields=[*real, "updated_at"])
        verb = "would update" if options["dry_run"] else "updated"
        self.stdout.write(f"{verb} {changed} application(s)")
