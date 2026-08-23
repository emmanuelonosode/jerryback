from django.test import TestCase


class SessionCaptureTests(TestCase):
    """
    The browser and the CDN were already sending the timezone, language, user
    agent, screen size, country and region. None of it had a column, so the
    processor dropped it and the admin could only show a city.
    """

    PAYLOAD = {
        "fingerprint": "fp-capture-test", "sessionId": "sess-capture-test",
        "event": "session_start", "path": "/homes-for-rent",
        "browser": "Chrome", "os": "macOS", "deviceType": "desktop",
        "userAgent": "Mozilla/5.0 Chrome/140", "language": "en-US",
        "timezone": "America/Chicago", "city": "Austin", "region": "TX",
        "country": "US", "ip": "203.0.113.0",
        "screenWidth": 2560, "screenHeight": 1440,
        "viewportWidth": 1280, "viewportHeight": 900,
        "referrer": "https://www.google.com/search?q=rentals",
    }

    def session(self, **overrides):
        from apps.analytics.models import RawTelemetryEvent, VisitorSession
        from apps.analytics.processing import process_spool
        RawTelemetryEvent.objects.create(payload={**self.PAYLOAD, **overrides})
        process_spool()
        return VisitorSession.objects.get(session_id=self.PAYLOAD["sessionId"])

    def test_it_records_where_and_when_they_are(self):
        s = self.session()
        self.assertEqual((s.city, s.region, s.country), ("Austin", "TX", "US"))
        self.assertEqual(s.timezone, "America/Chicago")
        self.assertEqual(s.language, "en-US")

    def test_it_records_what_they_used(self):
        s = self.session()
        self.assertEqual((s.screen_width, s.screen_height), (2560, 1440))
        self.assertEqual((s.viewport_width, s.viewport_height), (1280, 900))
        self.assertIn("Chrome", s.user_agent)

    def test_it_records_where_they_came_from(self):
        self.assertIn("google.com", self.session().referrer)

    def test_the_address_is_stored_truncated(self):
        # The last octet identifies a device rather than a place.
        self.assertEqual(self.session().ip_address, "203.0.113.0")

    def test_a_nonsense_screen_size_does_not_kill_the_batch(self):
        # These come from the browser, so a hostile client can send anything,
        # and an out-of-range integer raises at write time.
        s = self.session(screenWidth=99999999, screenHeight="not-a-number")
        self.assertIsNone(s.screen_width)
        self.assertIsNone(s.screen_height)

    def test_missing_geography_is_left_blank_rather_than_guessed(self):
        s = self.session(city="", region="", country="")
        self.assertEqual((s.city, s.region, s.country), ("", "", ""))
