import tempfile
from datetime import time, timedelta

from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from .models import TourRequest

REQUEST_URL = "/api/v1/viewings/request/"


def tomorrow() -> str:
    return (timezone.localdate() + timedelta(days=1)).isoformat()


def photo(name="id.jpg") -> SimpleUploadedFile:
    return SimpleUploadedFile(name, b"\xff\xd8\xff\xe0fake-jpeg", content_type="image/jpeg")


class TourWindowTests(TestCase):
    def setUp(self):
        cache.clear()  # the tour throttle is 10/hour
        self.client = APIClient()

    def post(self, **extra):
        body = {"name": "Dana Okafor", "email": "dana@example.com", "preferredDate": tomorrow()}
        body.update(extra)
        return self.client.post(REQUEST_URL, body, format="json")

    def test_hourly_slot_is_stored_as_real_times_and_a_label(self):
        response = self.post(timeStart="09:00", timeEnd="10:00")
        self.assertEqual(response.status_code, 201, response.content)
        tour = TourRequest.objects.get()
        self.assertEqual((tour.time_start, tour.time_end), (time(9), time(10)))
        self.assertFalse(tour.time_is_custom)
        self.assertEqual(tour.preferred_time, "9:00 AM - 10:00 AM")
        self.assertEqual(str(tour.public_id), response.json()["id"])

    def test_custom_range_is_flagged_for_staff(self):
        response = self.post(timeStart="13:30", timeEnd="15:00", customTime=True)
        self.assertEqual(response.status_code, 201, response.content)
        tour = TourRequest.objects.get()
        self.assertTrue(tour.time_is_custom)
        self.assertEqual(tour.time_window_label(), "1:30 PM - 3:00 PM")

    def test_range_in_preferred_time_is_understood(self):
        self.assertEqual(self.post(preferredTime="17:00-18:00").status_code, 201)
        self.assertEqual(TourRequest.objects.get().time_start, time(17))

    def test_older_day_part_still_books(self):
        # A cached copy of the old page must not lose the request.
        self.assertEqual(self.post(preferredTime="afternoon").status_code, 201)
        tour = TourRequest.objects.get()
        self.assertIsNone(tour.time_start)
        self.assertEqual(tour.time_window_label(), "afternoon")

    def test_backwards_window_is_refused(self):
        response = self.post(timeStart="11:00", timeEnd="10:00")
        self.assertEqual(response.status_code, 400)
        self.assertIn("after the start", response.json()["detail"])
        self.assertFalse(TourRequest.objects.exists())

    def test_window_outside_tour_hours_is_refused(self):
        self.assertEqual(self.post(timeStart="05:00", timeEnd="06:00").status_code, 400)
        self.assertEqual(self.post(timeStart="20:00", timeEnd="22:00").status_code, 400)

    def test_half_a_window_is_refused(self):
        self.assertEqual(self.post(timeStart="09:00").status_code, 400)

    def test_past_date_is_refused(self):
        past = (timezone.localdate() - timedelta(days=5)).isoformat()
        self.assertEqual(
            self.post(preferredDate=past, timeStart="09:00", timeEnd="10:00").status_code, 400,
        )


@override_settings(PRIVATE_UPLOAD_ROOT=tempfile.mkdtemp())
class TourIdUploadTests(TestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient()
        response = self.client.post(
            REQUEST_URL,
            {"name": "Dana", "email": "dana@example.com", "preferredDate": tomorrow(),
             "timeStart": "09:00", "timeEnd": "10:00"},
            format="json",
        )
        self.public_id = response.json()["id"]
        self.url = f"/api/v1/viewings/{self.public_id}/id/"

    def test_front_and_back_are_both_stored(self):
        response = self.client.post(self.url, {"idFront": photo(), "idBack": photo()}, format="multipart")
        self.assertEqual(response.status_code, 200, response.content)
        tour = TourRequest.objects.get()
        self.assertTrue(tour.id_front_url and tour.id_back_url)

    def test_front_alone_is_refused_and_nothing_is_kept(self):
        response = self.client.post(self.url, {"idFront": photo()}, format="multipart")
        self.assertEqual(response.status_code, 400)
        self.assertIn("back", response.json()["detail"])
        self.assertEqual(TourRequest.objects.get().id_front_url, "")

    def test_a_retry_may_send_only_the_missing_side(self):
        TourRequest.objects.update(id_front_url="already-there.jpg")
        response = self.client.post(self.url, {"idBack": photo()}, format="multipart")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(TourRequest.objects.get().id_back_url)
