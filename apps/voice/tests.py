import json
from datetime import timedelta

from django.test import TestCase, override_settings
from django.utils import timezone

from apps.core.money import dollars
from apps.crm.models import ApplicationStatus, Lead, LeadSource, RentalApplication
from apps.properties.models import PropertyStatus
from apps.properties.tests import make_property
from apps.scheduler.models import TourRequest, TourStatus

TOKEN = "test-voice-token"
URL = "/api/v1/voice/mcp"


@override_settings(VOICE_MCP_TOKEN=TOKEN)
class McpTestCase(TestCase):
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
            "search_homes", "get_home_details", "book_tour", "save_caller_details",
            "check_application_status", "get_leasing_policies",
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


class BookTourTests(McpTestCase):
    def test_books_a_request_that_a_person_confirms(self):
        home = make_property()
        tomorrow = (timezone.localdate() + timedelta(days=1)).isoformat()
        data, err = self.call(
            "book_tour", home_slug=home.slug, full_name="Ada Lovelace",
            phone="(704) 555-0100", preferred_date=tomorrow, preferred_time="morning",
        )
        self.assertFalse(err)
        tour = TourRequest.objects.get()
        self.assertEqual(tour.status, TourStatus.PENDING_REVIEW)
        self.assertEqual(tour.property, home)
        self.assertEqual(tour.lead.source, LeadSource.PHONE_AGENT)
        self.assertIn("not yet confirmed", data["tell_the_caller"])

    def test_a_past_date_or_short_phone_is_refused(self):
        home = make_property()
        yesterday = (timezone.localdate() - timedelta(days=1)).isoformat()
        _, err = self.call("book_tour", home_slug=home.slug, full_name="A", phone="7045550100",
                           preferred_date=yesterday)
        self.assertTrue(err)
        _, err = self.call("book_tour", home_slug=home.slug, full_name="A", phone="555",
                           preferred_date=timezone.localdate().isoformat())
        self.assertTrue(err)
        self.assertFalse(TourRequest.objects.exists())


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
