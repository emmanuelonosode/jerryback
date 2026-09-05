"""
Delete ID documents once they have served their purpose.

THE POLICY EXISTED AND NOTHING RAN IT. `TourRequest.ready_to_purge` and
`purge_ids` have been on the model all along, and no command, cron entry or
call site anywhere invoked either. Until now that was harmless because nothing
uploaded an ID; the moment the tour wizard started accepting them it would
have meant government identity documents accumulating on disk indefinitely
with a retention policy that only existed on paper.

Runs daily. A document is deleted 24 hours after the request it belongs to was
reviewed - long enough for a second look, short enough that we are not holding
someone's licence a week after they saw the house.
"""

from django.core.management.base import BaseCommand

from apps.scheduler.models import TourRequest


class Command(BaseCommand):
    help = "Delete tour ID documents for requests reviewed more than N hours ago."

    def add_arguments(self, parser):
        parser.add_argument("--hours", type=int, default=24)
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would be deleted without deleting it.",
        )

    def handle(self, *args, **options):
        hours = options["hours"]
        due = TourRequest.ready_to_purge(older_than_hours=hours)
        count = due.count()

        if options["dry_run"]:
            for tour in due:
                self.stdout.write(f"  would purge {tour.public_id} ({tour.full_name})")
            self.stdout.write(f"dry run: {count} request(s) hold documents due for deletion")
            return

        purged = 0
        for tour in due:
            tour.purge_ids()
            purged += 1

        self.stdout.write(f"purged IDs from {purged} tour request(s), reviewed over {hours}h ago")
