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

    class Meta:
        db_table = "outbound_emails"
        ordering = ["send_after"]
        indexes = [models.Index(fields=["status", "send_after"])]

    def __str__(self) -> str:
        return f"{self.subject} -> {self.to_email}"


def queue_email(*, to_email: str, subject: str, body_text: str, template: str = "") -> "OutboundEmail | None":
    """
    Queue a message with the branded HTML built for it.

    ONE ENTRY POINT, so no caller can queue mail that arrives unbranded. The
    plain-text body stays the source of truth and is sent alongside as the
    text/plain alternative - clients that refuse HTML, and screen readers that
    prefer text, get the same words rather than a "view this in your browser"
    stub.
    """
    from .branding import render_email_html

    address = (to_email or "").strip()
    if not address:
        return None

    return OutboundEmail.objects.create(
        to_email=address,
        subject=subject,
        body_text=body_text,
        body_html=render_email_html(subject, body_text),
        template=template,
    )
