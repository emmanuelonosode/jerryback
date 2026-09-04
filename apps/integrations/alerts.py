"""
Telling the business a lead has arrived.

WHY THIS EXISTS. Nine leads, five tour requests and three applications were
sitting in the database - real people with real phone numbers, some of them
days old - and nobody had been told about a single one of them. The site
captured every one correctly. Django stored every one correctly. There was
simply no code anywhere that notified a human, and `ADMINS` was an empty list,
so the business's own read of the situation was "the popup is broken and we
are not getting leads".

Nothing was broken. Nobody was listening.

TWO RULES, and the second one matters more than the first.

A FAILED ALERT MUST NEVER FAIL THE SUBMISSION. Every call here is wrapped: if
the mail server is slow, refuses the connection, or the recipient list is
misconfigured, the visitor still gets their "thank you" and the lead is still
in the database. Losing a lead because we could not send ourselves an email
about it would be a worse bug than the one this fixes. That is why the whole
body of `notify_staff` is inside a try, and why it returns a bool rather than
raising.

IT SENDS NOW, NOT ON THE QUEUE. `send_queued_email` runs on a cron tick, and a
callback request that reaches somebody twenty minutes later has usually gone
cold - the person is on a competitor's site by then. These are short messages
to one or two internal addresses, so the cost of sending inline is small and
the value of arriving immediately is the entire point.
"""

import logging

from django.conf import settings

logger = logging.getLogger(__name__)


def staff_recipients() -> list[str]:
    """
    Who hears about a new lead.

    From `STAFF_ALERT_EMAILS` so it can change without a deploy - the person
    who should be woken up by a lead is a business decision, not a code one.
    Falls back to the address the site already publishes as its own, which is
    always deliverable because it is the same mailbox the site sends from.
    """
    raw = getattr(settings, "STAFF_ALERT_EMAILS", "") or ""
    addresses = [a.strip() for a in raw.replace(";", ",").replace("|", ",").split(",")]
    addresses = [a for a in addresses if a and "@" in a]
    if addresses:
        return addresses

    fallback = getattr(settings, "DEFAULT_FROM_EMAIL", "") or ""
    # DEFAULT_FROM_EMAIL is usually "Name <addr@host>"; take the address.
    if "<" in fallback and ">" in fallback:
        fallback = fallback.split("<", 1)[1].split(">", 1)[0]
    return [fallback.strip()] if "@" in fallback else []


def admin_link(path: str) -> str:
    """A deep link into the admin record, so acting on a lead is one click."""
    base = (getattr(settings, "PUBLIC_ADMIN_URL", "") or "").rstrip("/")
    if not base:
        return ""
    admin_path = (getattr(settings, "ADMIN_PATH", "") or "admin/").strip("/")
    return f"{base}/{admin_path}/{path.strip('/')}/"


def notify_staff(*, subject: str, body: str, kind: str = "") -> bool:
    """
    Email the team about something a visitor just did. Never raises.

    Returns True if at least one message was queued, False otherwise - the
    caller ignores it, but it makes the function testable without a mail
    server and makes a silent misconfiguration visible in the logs.
    """
    try:
        from .models import queue_email

        recipients = staff_recipients()
        if not recipients:
            logger.warning("staff alert not sent (%s): no recipients configured", kind)
            return False

        sent = False
        for address in recipients:
            queue_email(
                to_email=address,
                subject=subject,
                body_text=body,
                template=f"staff-alert-{kind}" if kind else "staff-alert",
                # A lead is only worth knowing about while it is warm.
                send_now=True,
            )
            sent = True
        return sent
    except Exception:
        # Deliberately broad. Whatever goes wrong here, the visitor's
        # submission has already succeeded and must not be rolled back.
        logger.exception("staff alert failed (%s)", kind)
        return False


def describe(pairs: list[tuple[str, object]]) -> str:
    """
    Format "Label: value" lines, dropping the ones with nothing in them.

    An alert full of "Phone: (blank)" is harder to read at a glance than one
    that simply omits what the person did not give us, and these are read on a
    phone, one-handed, deciding whether to call back now.
    """
    lines = []
    for label, value in pairs:
        text = "" if value is None else str(value).strip()
        if text:
            lines.append(f"{label}: {text}")
    return "\n".join(lines)
