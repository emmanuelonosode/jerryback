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
