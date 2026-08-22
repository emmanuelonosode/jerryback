from django.conf import settings
from django.core.mail import EmailMultiAlternatives, get_connection
from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.integrations.models import EmailStatus, OutboundEmail

MAX_ATTEMPTS = 5


class Command(BaseCommand):
    """
    Send whatever is queued.

    NOTHING SENT THE QUEUE BEFORE THIS. `OutboundEmail` rows were created and
    then sat there: an approved applicant was told by the admin that they had
    been emailed, and no email existed. Run it on a schedule, every minute or
    two.

    Sent as multipart: the branded HTML plus the plain text it was built from.
    A client that refuses HTML gets the same words rather than a stub.
    """

    help = "Dispatch queued outbound email."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=50)
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        now = timezone.now()
        queued = OutboundEmail.objects.filter(
            status=EmailStatus.QUEUED, send_after__lte=now, attempts__lt=MAX_ATTEMPTS,
        ).order_by("created_at")[: options["limit"]]

        if options["dry_run"]:
            for message in queued:
                self.stdout.write(f"  would send {message.subject!r} to {message.to_email}")
            self.stdout.write(self.style.WARNING(f"{len(queued)} message(s) queued."))
            return

        sent = failed = 0
        # One connection for the batch rather than one per message.
        connection = get_connection()
        for message in queued:
            try:
                email = EmailMultiAlternatives(
                    subject=message.subject,
                    body=message.body_text,
                    from_email=settings.DEFAULT_FROM_EMAIL,
                    to=[message.to_email],
                    connection=connection,
                )
                if message.body_html:
                    email.attach_alternative(message.body_html, "text/html")
                email.send()
            except Exception as error:  # noqa: BLE001 - recorded, not swallowed
                message.attempts += 1
                message.last_error = str(error)[:2000]
                # Only give up once the attempts are exhausted; a transient SMTP
                # failure must not bin a move-in notice.
                if message.attempts >= MAX_ATTEMPTS:
                    message.status = EmailStatus.FAILED
                message.save(update_fields=["attempts", "last_error", "status"])
                failed += 1
                continue

            message.status = EmailStatus.SENT
            message.sent_at = timezone.now()
            message.attempts += 1
            message.save(update_fields=["status", "sent_at", "attempts"])
            sent += 1

        self.stdout.write(self.style.SUCCESS(f"Sent {sent}, failed {failed}."))
