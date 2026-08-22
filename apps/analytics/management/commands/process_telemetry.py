from django.core.management.base import BaseCommand

from apps.analytics.processing import process_spool


class Command(BaseCommand):
    help = "Fold spooled telemetry into visitors, sessions and page visits."

    def add_arguments(self, parser):
        parser.add_argument("--batch-size", type=int, default=200)

    def handle(self, *args, **options):
        result = process_spool(options["batch_size"])
        self.stdout.write(
            f"processed={result['processed']} failed={result['failed']} parked={result['parked']}",
        )
