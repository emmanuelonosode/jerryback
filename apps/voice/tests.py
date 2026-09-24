import json
from datetime import timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.test import TestCase, override_settings
from django.utils import timezone

from apps.core.money import dollars
from apps.crm.models import ApplicationStatus, Lead, LeadSource, RentalApplication
from apps.properties.models import PropertyStatus
from apps.properties.tests import make_property
from apps.integrations.models import OutboundEmail
from apps.scheduler.models import TourRequest, TourStatus, Viewing, ViewingStatus

TOKEN = "test-voice-token"
URL = "/api/v1/voice/mcp"


def days(n):
    return (timezone.localdate() + timedelta(days=n)).isoformat()


@override_settings(VOICE_MCP_TOKEN=TOKEN)
class McpTestCase(TestCase):
    def setUp(self):
        # Staff alerts send on a thread, which fights the test database for
        # its lock. What is queued is asserted on; the send itself is not.
        patcher = patch("apps.integrations.alerts.deliver_in_background")
        patcher.start()
        self.addCleanup(patcher.stop)

    def rpc(self, method, params=None, msg_id=1, token=TOKEN):
        body = {"jsonrpc": "2.0", "id": msg_id, "method": method}
        if params is not None:
            body["params"] = params
        headers = {"HTTP_AUTHORIZATION": f"Bearer {token}"} if token else {}
        return self.client.post(URL, json.dumps(body), content_type="application/json", **headers)

    def call(self, tool, **arguments):
        response = self.rpc("tools/call", {"name": tool, "arguments": arguments})
        self.assertEqual(response.status_code, 200)
        result = response.json()["result"]
        return json.loads(result["content"][0]["text"]), result["isError"]


class ProtocolTests(McpTestCase):
    def test_unset_token_means_the_endpoint_does_not_exist(self):
        # A forgotten env var must fail closed, not open a write path to the CRM.
        with override_settings(VOICE_MCP_TOKEN=""):
            self.assertEqual(self.rpc("ping").status_code, 404)

    def test_wrong_or_missing_token_is_refused(self):
        self.assertEqual(self.rpc("ping", token="nope").status_code, 401)
        self.assertEqual(self.rpc("ping", token="").status_code, 401)

    def test_initialize_negotiates_version_and_lists_tools(self):
        init = self.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}}).json()
        self.assertEqual(init["result"]["protocolVersion"], "2025-06-18")
        self.assertIn("tools", init["result"]["capabilities"])

        names = {t["name"] for t in self.rpc("tools/list").json()["result"]["tools"]}
        self.assertEqual(names, {
            "search_homes", "get_home_details", "find_tour_times", "book_tour", "check_my_tours",
            "reschedule_tour", "cancel_tour", "save_caller_details", "check_application_status",
            "get_leasing_policies",
        })

    def test_notifications_get_202_and_no_body(self):
        response = self.client.post(
            URL, json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
            content_type="application/json", HTTP_AUTHORIZATION=f"Bearer {TOKEN}",
        )
        self.assertEqual(response.status_code, 202)

    def test_get_is_405_because_no_stream_is_offered(self):
        response = self.client.get(URL, HTTP_AUTHORIZATION=f"Bearer {TOKEN}")
        self.assertEqual(response.status_code, 405)

    def test_unknown_method_is_a_jsonrpc_error(self):
        self.assertEqual(self.rpc("resources/list").json()["error"]["code"], -32601)


class HomeToolTests(McpTestCase):
    def test_search_filters_and_quotes_the_all_in_price(self):
        make_property(bedrooms=3, price_cents=dollars(1500))
        make_property(bedrooms=1, price_cents=dollars(900), address="1 Other St")
        make_property(bedrooms=4, price_cents=dollars(2000), status=PropertyStatus.LEASED, leased_at=timezone.now(),
                      address="2 Gone St")

        data, err = self.call("search_homes", city="Charlotte", min_bedrooms=2)
        self.assertFalse(err)
        self.assertEqual(data["count_shown"], 1)
        self.assertEqual(data["homes"][0]["total_monthly"], "$1,500")

    def test_details_by_address_and_unpublished_homes_are_invisible(self):
        home = make_property()
        make_property(address="9 Hidden Rd", is_published=False)

        data, err = self.call("get_home_details", address="8043 Kenilworth")
        self.assertFalse(err)
        self.assertEqual(data["slug"], home.slug)

        _, err = self.call("get_home_details", address="9 Hidden Rd")
        self.assertTrue(err)


@patch("apps.voice.tools.deliver_in_background")
class BookTourTests(McpTestCase):
    def test_an_in_person_tour_is_a_request_a_person_confirms(self, _deliver):
        home = make_property()
        data, err = self.call(
            "book_tour", home_slug=home.slug, tour_type="in-person", full_name="Ada Lovelace",
            phone="(704) 555-0100", preferred_date=days(1), preferred_time="morning",
        )
        self.assertFalse(err)
        tour = TourRequest.objects.get()
        self.assertEqual(tour.status, TourStatus.PENDING_REVIEW)
        self.assertIsNone(tour.viewing)
        self.assertEqual(tour.lead.source, LeadSource.PHONE_AGENT)
        self.assertIn("not yet confirmed", data["tell_the_caller"])

    def test_a_past_date_or_short_phone_is_refused(self, _deliver):
        home = make_property()
        _, err = self.call("book_tour", home_slug=home.slug, tour_type="in-person", full_name="A",
                           phone="7045550100", preferred_date=days(-1))
        self.assertTrue(err)
        _, err = self.call("book_tour", home_slug=home.slug, tour_type="in-person", full_name="A",
                           phone="555", preferred_date=days(1))
        self.assertTrue(err)
        self.assertFalse(TourRequest.objects.exists())


@patch("apps.voice.tools.deliver_in_background")
class SelfTourTests(McpTestCase):
    def book(self, home, when="10:00", **over):
        return self.call("book_tour", **{
            "home_slug": home.slug, "tour_type": "self-tour", "full_name": "Ada Lovelace",
            "phone": "704-555-0100", "email": "ada@example.com", "preferred_date": days(3),
            "time": when, **over,
        })

    def test_times_are_offered_in_the_homes_own_timezone(self, _deliver):
        home = make_property(state="AZ", allow_selfshow=True)
        data, _ = self.call("find_tour_times", home_slug=home.slug, date=days(3))
        self.assertEqual(data["timezone"], "America/Phoenix")
        self.assertEqual(data["open_times"][0], {"time": "09:00", "say": "9:00 AM"})

        self.book(home, when="09:00")
        viewing = Viewing.objects.get()
        # 9am in Phoenix, which keeps no daylight saving, is always 16:00 UTC.
        self.assertEqual(viewing.scheduled_at.astimezone(ZoneInfo("UTC")).hour, 16)

    def test_a_booked_slot_is_held_and_cannot_be_double_booked(self, _deliver):
        home = make_property(allow_selfshow=True)
        data, err = self.book(home)
        self.assertFalse(err)
        self.assertEqual(data["status"], "booked")
        tour = TourRequest.objects.get()
        self.assertEqual(tour.viewing.status, ViewingStatus.SCHEDULED)

        _, err = self.book(home, phone="7045550199")
        self.assertTrue(err)
        times = [t["time"] for t in self.call("find_tour_times", home_slug=home.slug, date=days(3))[0]["open_times"]]
        self.assertNotIn("10:00", times)
        # Back-to-back is fine; overlapping is not.
        self.assertIn("09:30", times)
        self.assertIn("10:30", times)

    def test_the_caller_is_emailed_an_id_link_and_never_given_a_code(self, _deliver):
        home = make_property(allow_selfshow=True)
        data, _ = self.book(home)
        tour = TourRequest.objects.get()
        mail = OutboundEmail.objects.get(to_email="ada@example.com")
        self.assertIn(f"/tour-id/{tour.public_id}", mail.body_text)
        self.assertNotIn("code is", data["tell_the_caller"].lower().replace("door code", ""))

    def test_a_home_not_set_up_for_self_showing_is_refused(self, _deliver):
        home = make_property(allow_selfshow=False)
        _, err = self.book(home)
        self.assertTrue(err)
        data, _ = self.call("find_tour_times", home_slug=home.slug, date=days(3))
        self.assertFalse(data["self_tour_available"])

    def test_looking_up_a_tour_needs_a_second_factor_and_hides_the_code(self, _deliver):
        home = make_property(allow_selfshow=True)
        self.book(home)
        Viewing.objects.update(access_code="123456")

        _, err = self.call("check_my_tours", phone="7045550100")
        self.assertTrue(err)
        data, _ = self.call("check_my_tours", phone="7045550100", last_name="Byron")
        self.assertEqual(data["tours"], [])
        data, _ = self.call("check_my_tours", phone="+1 704 555 0100", last_name="lovelace")
        self.assertEqual(len(data["tours"]), 1)
        self.assertNotIn("123456", json.dumps(data))

    def test_reschedule_moves_the_slot_and_voids_the_old_code(self, _deliver):
        home = make_property(allow_selfshow=True)
        booked, _ = self.book(home)
        Viewing.objects.update(access_code="123456")

        data, err = self.call("reschedule_tour", phone="7045550100", reference=booked["reference"],
                              new_date=days(4), new_time="15:00")
        self.assertFalse(err)
        viewing = Viewing.objects.get()
        self.assertEqual(viewing.access_code, "")
        self.assertEqual(viewing.scheduled_at.astimezone(ZoneInfo("America/New_York")).hour, 15)
        # The old slot is free again.
        times = [t["time"] for t in self.call("find_tour_times", home_slug=home.slug, date=days(3))[0]["open_times"]]
        self.assertIn("10:00", times)

    def test_cancel_frees_the_slot_and_starts_the_id_purge_clock(self, _deliver):
        home = make_property(allow_selfshow=True)
        self.book(home)
        TourRequest.objects.update(id_front_url="front.jpg")

        _, err = self.call("cancel_tour", phone="7045550100", email="ada@example.com")
        self.assertFalse(err)
        tour = TourRequest.objects.get()
        self.assertEqual(tour.status, TourStatus.CANCELLED)
        self.assertEqual(tour.viewing.status, ViewingStatus.CANCELLED)
        TourRequest.objects.update(reviewed_at=timezone.now() - timedelta(hours=25))
        self.assertIn(tour, TourRequest.ready_to_purge())
        # And a cancelled tour no longer shows up as theirs.
        data, _ = self.call("check_my_tours", phone="7045550100", email="ada@example.com")
        self.assertEqual(data["tours"], [])


class CallerTests(McpTestCase):
    def test_a_repeat_caller_is_one_lead(self):
        self.call("save_caller_details", phone="7045550100", summary="Wants 3 bed")
        self.call("save_caller_details", phone="7045550100", full_name="Ada", summary="Called back")
        lead = Lead.objects.get()
        self.assertEqual(lead.full_name, "Ada")
        # A note, not a CALL: staff still owe this person a human conversation.
        self.assertIsNone(lead.last_contacted_at)


class ApplicationStatusTests(McpTestCase):
    def make_application(self, **over):
        defaults = dict(
            email="ada@example.com", cell_phone="704-555-0100",
            status=ApplicationStatus.SUBMITTED, application_fee_cents=dollars(55),
        )
        return RentalApplication.objects.create(**{**defaults, **over})

    def test_needs_the_email_and_phone_to_match_the_same_application(self):
        self.make_application()
        data, _ = self.call("check_application_status", email="ada@example.com", phone="7045550199")
        self.assertFalse(data["found"])
        data, _ = self.call("check_application_status", email="ADA@example.com", phone="+1 704 555 0100")
        self.assertTrue(data["found"])
        self.assertIn("review", data["stage"])

    def test_a_decline_is_never_read_out(self):
        self.make_application(status=ApplicationStatus.REJECTED, decision_reason="Income")
        data, _ = self.call("check_application_status", email="ada@example.com", phone="7045550100")
        self.assertNotIn("declin", json.dumps(data).lower())
        self.assertNotIn("income", json.dumps(data).lower())
