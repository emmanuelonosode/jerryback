from django.test import TestCase
from django.urls import reverse

from apps.accounts.models import User

from .models import EmailStatus, OutboundEmail, queue_email


class BrandingTests(TestCase):
    """Every message carries the header, footer and the supplied logo."""

    def test_queueing_builds_the_branded_html(self):
        email = queue_email(to_email="a@b.com", subject="Hi", body_text="Body.")
        self.assertIn("logo-lockup-white", email.body_html)
        self.assertIn("Equal Housing Opportunity", email.body_html)

    def test_the_plain_text_is_kept_as_well(self):
        # Sent as the text/plain alternative, so a client that refuses HTML
        # gets the same words rather than a "view in browser" stub.
        email = queue_email(to_email="a@b.com", subject="Hi", body_text="The words.")
        self.assertEqual(email.body_text, "The words.")

    def test_an_empty_address_queues_nothing(self):
        self.assertIsNone(queue_email(to_email="  ", subject="Hi", body_text="B"))


class ComposeTests(TestCase):
    """
    The compose screen. Before it existed the admin registered this model with
    `pass`: staff could see that mail was queued and had no way to send any.
    """

    def setUp(self):
        self.staff = User.objects.create_user(
            email="staff@skelton.test", password="Compose!23",
            first_name="S", last_name="T",
        )
        self.staff.is_staff = self.staff.is_superuser = True
        self.staff.save()
        self.client.force_login(self.staff)
        self.url = reverse("admin:integrations_outboundemail_compose")

    def test_the_page_loads(self):
        self.assertEqual(self.client.get(self.url).status_code, 200)

    def test_the_changelist_links_to_it(self):
        listing = reverse("admin:integrations_outboundemail_changelist")
        self.assertContains(self.client.get(listing), self.url)

    def test_typed_addresses_are_queued(self):
        self.client.post(self.url, {
            "extra_emails": "one@example.com, two@example.com",
            "subject": "Subject", "body": "Body",
        })
        self.assertEqual(
            sorted(OutboundEmail.objects.values_list("to_email", flat=True)),
            ["one@example.com", "two@example.com"],
        )

    def test_an_existing_account_can_be_picked(self):
        self.client.post(self.url, {
            "recipients": [str(self.staff.pk)], "subject": "S", "body": "B",
        })
        self.assertEqual(OutboundEmail.objects.get().to_email, self.staff.email)

    def test_the_same_person_is_not_written_to_twice(self):
        # Picking the account AND typing the address is an easy mistake.
        self.client.post(self.url, {
            "recipients": [str(self.staff.pk)],
            "extra_emails": self.staff.email.upper(),
            "subject": "S", "body": "B",
        })
        self.assertEqual(OutboundEmail.objects.count(), 1)

    def test_one_row_per_recipient(self):
        # Not one row with several addresses: per-address delivery state, and
        # nobody sees who else was written to.
        self.client.post(self.url, {
            "extra_emails": "a@x.com\nb@x.com\nc@x.com",
            "subject": "S", "body": "B",
        })
        self.assertEqual(OutboundEmail.objects.count(), 3)

    def test_sending_to_nobody_is_refused(self):
        self.client.post(self.url, {"subject": "S", "body": "B"})
        self.assertEqual(OutboundEmail.objects.count(), 0)

    def test_a_malformed_address_is_refused(self):
        self.client.post(self.url, {
            "extra_emails": "not-an-email", "subject": "S", "body": "B",
        })
        self.assertEqual(OutboundEmail.objects.count(), 0)

    def test_composed_mail_is_branded_like_the_automatic_kind(self):
        self.client.post(self.url, {
            "extra_emails": "a@x.com", "subject": "S", "body": "B",
        })
        self.assertIn("logo-lockup-white", OutboundEmail.objects.get().body_html)


class PreviewAndRequeueTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            email="staff2@skelton.test", password="Compose!23",
            first_name="S", last_name="T",
        )
        self.staff.is_staff = self.staff.is_superuser = True
        self.staff.save()
        self.client.force_login(self.staff)
        self.email = queue_email(to_email="a@b.com", subject="Hi", body_text="Body.")

    def test_preview_shows_what_was_actually_queued(self):
        # The stored HTML, not a re-render: a later wording change must not
        # rewrite what a sent message said.
        self.email.body_html = "<p>exactly this</p>"
        self.email.save(update_fields=["body_html"])
        url = reverse("admin:integrations_outboundemail_preview", args=[self.email.pk])
        self.assertContains(self.client.get(url), "exactly this")

    def test_a_failed_message_can_be_sent_again(self):
        self.email.status = EmailStatus.FAILED
        self.email.attempts = 5
        self.email.last_error = "smtp refused"
        self.email.save()

        self.client.post(
            reverse("admin:integrations_outboundemail_changelist"),
            {"action": "requeue_selected", "_selected_action": [str(self.email.pk)]},
        )
        self.email.refresh_from_db()
        self.assertEqual(self.email.status, EmailStatus.QUEUED)
        # Reset, or a message that exhausted its retries against a broken server
        # stays failed forever after the server is fixed.
        self.assertEqual(self.email.attempts, 0)


class InstantDeliveryTests(TestCase):
    """
    Mail somebody is sitting and waiting for goes out before the response,
    without giving up the guarantee that a broken mail server cannot fail the
    request that triggered it.
    """

    def test_send_now_delivers_immediately(self):
        from django.test import override_settings
        with override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend"):
            message = queue_email(
                to_email="a@b.com", subject="Code", body_text="123456", send_now=True,
            )
        self.assertEqual(message.status, EmailStatus.SENT)
        self.assertIsNotNone(message.sent_at)

    def test_without_send_now_it_waits_for_the_queue(self):
        message = queue_email(to_email="a@b.com", subject="Bulk", body_text="Body")
        self.assertEqual(message.status, EmailStatus.QUEUED)
        self.assertIsNone(message.sent_at)

    def test_a_broken_mail_server_leaves_it_queued_rather_than_lost(self):
        from django.test import override_settings
        # Port 1 refuses instantly; this is the SMTP-is-down case.
        with override_settings(
            EMAIL_BACKEND="django.core.mail.backends.smtp.EmailBackend",
            EMAIL_HOST="127.0.0.1", EMAIL_PORT=1,
        ):
            message = queue_email(
                to_email="a@b.com", subject="Code", body_text="123456", send_now=True,
            )
        self.assertEqual(message.status, EmailStatus.QUEUED)
        self.assertIn("refused", message.last_error.lower())

    def test_a_broken_mail_server_does_not_raise(self):
        from django.test import override_settings
        with override_settings(
            EMAIL_BACKEND="django.core.mail.backends.smtp.EmailBackend",
            EMAIL_HOST="127.0.0.1", EMAIL_PORT=1,
        ):
            # The whole reason mail was queued: an SMTP timeout during
            # registration must not turn a 200 into a 504 after the account
            # already exists.
            queue_email(to_email="a@b.com", subject="S", body_text="B", send_now=True)


class OtpDeliveryTests(TestCase):
    def test_a_verification_code_is_sent_before_the_response_returns(self):
        from django.test import override_settings
        with override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend"):
            response = self.client.post(
                "/api/v1/auth/register/",
                {"email": "newcomer@example.com", "password": "Skelton8",
                 "first_name": "New", "last_name": "Renter"},
                content_type="application/json",
            )
        self.assertEqual(response.status_code, 202)
        sent = OutboundEmail.objects.get(template="otp")
        # Not left for the next cron tick: somebody is looking at the code field.
        self.assertEqual(sent.status, EmailStatus.SENT)

    def test_an_eight_character_password_is_accepted(self):
        from django.test import override_settings
        with override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend"):
            response = self.client.post(
                "/api/v1/auth/register/",
                {"email": "shortpw@example.com", "password": "Skelton8",
                 "first_name": "A", "last_name": "B"},
                content_type="application/json",
            )
        self.assertEqual(response.status_code, 202)


class ConcurrentSendTests(TestCase):
    """
    A one-minute cron and a slow mail server overlap routinely. Nothing held a
    lock, so both runs would read the same QUEUED rows and both would send them.
    """

    def test_a_claimed_message_is_not_sent_twice(self):
        from django.core.management import call_command
        from django.test import override_settings
        from io import StringIO

        message = queue_email(to_email="a@b.com", subject="S", body_text="B")

        # Stand in for a run that has already claimed it and is mid-send.
        OutboundEmail.objects.filter(pk=message.pk).update(status=EmailStatus.SENDING)

        out = StringIO()
        with override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend"):
            call_command("send_queued_email", stdout=out)

        from django.core import mail
        self.assertEqual(len(mail.outbox), 0)

    def test_a_message_stranded_by_a_crash_is_recovered(self):
        from datetime import timedelta
        from django.core.management import call_command
        from django.test import override_settings
        from django.utils import timezone
        from io import StringIO

        message = queue_email(to_email="a@b.com", subject="S", body_text="B")
        OutboundEmail.objects.filter(pk=message.pk).update(status=EmailStatus.SENDING)
        # Older than the recovery window: the run that claimed it is gone.
        OutboundEmail.objects.filter(pk=message.pk).update(
            updated_at=timezone.now() - timedelta(minutes=30),
        )

        with override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend"):
            call_command("send_queued_email", stdout=StringIO())

        message.refresh_from_db()
        self.assertEqual(message.status, EmailStatus.SENT)
