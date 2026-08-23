"""Outbound mail queue."""

import uuid

from django.db import models
from django.utils import timezone


class EmailStatus(models.TextChoices):
    QUEUED = "QUEUED", "Queued"
    SENDING = "SENDING", "Sending"
    SENT = "SENT", "Sent"
    FAILED = "FAILED", "Failed"


class OutboundEmail(models.Model):
    """
    Mail is queued, never sent on the request thread.

    An SMTP timeout during registration would otherwise turn a 200 into a 504
    after the account was already created — the user sees a failure, retries,
    and hits "email already registered".
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    to_email = models.EmailField()
    subject = models.CharField(max_length=300)
    body_text = models.TextField()
    body_html = models.TextField(blank=True, default="")
    template = models.CharField(max_length=80, blank=True, default="")
    status = models.CharField(max_length=10, choices=EmailStatus.choices, default=EmailStatus.QUEUED, db_index=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    last_error = models.TextField(blank=True, default="")
    send_after = models.DateTimeField(default=timezone.now)
    sent_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    # Needed to spot a message stranded mid-send by a run that died: without a
    # last-touched time there is no way to tell "sending right now" from
    # "claimed an hour ago by a process that is gone".
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "outbound_emails"
        ordering = ["send_after"]
        indexes = [models.Index(fields=["status", "send_after"])]

    def __str__(self) -> str:
        return f"{self.subject} -> {self.to_email}"


def deliver_now(message: "OutboundEmail") -> bool:
    """
    Try to send this one immediately. Never raises.

    FOR MAIL SOMEBODY IS SITTING AND WAITING FOR - a verification code, most of
    all. Waiting on the next cron tick to receive a code is the difference
    between finishing a registration and abandoning it.

    A FAILURE HERE COSTS NOTHING, which is what makes it safe to try. The row is
    already queued before this runs, so a timeout, a refused connection or an
    unconfigured mail server just leaves it for `send_queued_email` to retry.
    That was the whole reason mail was queued rather than sent inline: an SMTP
    timeout during registration would otherwise turn a 200 into a 504 after the
    account had already been created. Attempting delivery and shrugging off the
    failure keeps that guarantee and removes the wait.
    """
    from django.conf import settings
    from django.core.mail import EmailMultiAlternatives, get_connection
    from django.utils import timezone as _tz

    try:
        # `fail_silently=False` so the error is caught here and recorded, rather
        # than swallowed by the backend and reported as a success.
        connection = get_connection(fail_silently=False, timeout=10)
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
    except Exception as error:  # noqa: BLE001 - recorded, and retried by cron
        OutboundEmail.objects.filter(pk=message.pk).update(
            attempts=models.F("attempts") + 1, last_error=str(error)[:2000],
        )
        return False

    OutboundEmail.objects.filter(pk=message.pk).update(
        status=EmailStatus.SENT, sent_at=_tz.now(), attempts=models.F("attempts") + 1,
    )
    return True


def queue_email(
    *,
    to_email: str,
    subject: str,
    body_text: str,
    template: str = "",
    send_now: bool = False,
) -> "OutboundEmail | None":
    """
    Queue a message with the branded HTML built for it.

    ONE ENTRY POINT, so no caller can queue mail that arrives unbranded. The
    plain-text body stays the source of truth and is sent alongside as the
    text/plain alternative - clients that refuse HTML, and screen readers that
    prefer text, get the same words rather than a "view this in your browser"
    stub.

    `send_now` additionally attempts delivery before returning. Use it only for
    mail somebody is actively waiting on; a bulk send should stay on the queue
    so one slow recipient cannot hold up a request.
    """
    from .branding import render_email_html

    address = (to_email or "").strip()
    if not address:
        return None

    message = OutboundEmail.objects.create(
        to_email=address,
        subject=subject,
        body_text=body_text,
        body_html=render_email_html(subject, body_text),
        template=template,
    )

    if send_now:
        deliver_now(message)
        message.refresh_from_db()

    return message
