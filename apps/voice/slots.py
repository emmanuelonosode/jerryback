"""
When a self-guided tour can happen.

TIMES ARE THE HOME'S LOCAL TIME, NOT THE SERVER'S. The catalogue spans Eastern
to Pacific plus Arizona, which ignores daylight saving; a caller in Phoenix who
says "two o'clock" means two o'clock at the house, and storing that as 2pm UTC
sends them to a locked door seven hours late. So every slot is built in the
home's zone and stored as an aware datetime.

ONE PARTY PER HOME PER SLOT. Two strangers let into the same empty house at
once is the thing a self-tour schedule exists to prevent, so a slot is free only
when no active viewing of that home overlaps it.
"""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from django.conf import settings
from django.utils import timezone

from apps.scheduler.models import ACTIVE_VIEWING_STATUSES, Viewing

# Each state's majority zone. The handful of split states (TN, FL panhandle,
# TX far west) are rounded to where most of the catalogue sits.
STATE_ZONES = {
    **dict.fromkeys(
        "CT DE DC FL GA IN KY ME MD MA MI NH NJ NY NC OH PA RI SC VT VA WV".split(),
        "America/New_York",
    ),
    **dict.fromkeys("AL AR IL IA KS LA MN MS MO NE ND OK SD TN TX WI".split(), "America/Chicago"),
    **dict.fromkeys("CO ID MT NM UT WY".split(), "America/Denver"),
    "AZ": "America/Phoenix",
    **dict.fromkeys("CA NV OR WA".split(), "America/Los_Angeles"),
    "AK": "America/Anchorage",
    "HI": "Pacific/Honolulu",
}


def zone_for(home) -> ZoneInfo:
    return ZoneInfo(STATE_ZONES.get((home.state or "").upper(), "America/New_York"))


def _config():
    return (
        settings.SELF_TOUR_START_HOUR,
        settings.SELF_TOUR_END_HOUR,
        timedelta(minutes=settings.SELF_TOUR_SLOT_MINUTES),
        timedelta(hours=settings.SELF_TOUR_MIN_NOTICE_HOURS),
    )


@dataclass(frozen=True)
class Slot:
    starts_at: datetime  # aware, in the home's zone

    @property
    def value(self) -> str:
        return self.starts_at.strftime("%H:%M")

    @property
    def label(self) -> str:
        return self.starts_at.strftime("%I:%M %p").lstrip("0")


def _busy(home, start: datetime, end: datetime, exclude_viewing=None):
    length = timedelta(minutes=settings.SELF_TOUR_SLOT_MINUTES)
    busy = Viewing.objects.filter(
        property=home, status__in=ACTIVE_VIEWING_STATUSES,
        scheduled_at__gt=start - length, scheduled_at__lt=end,
    )
    if exclude_viewing is not None:
        busy = busy.exclude(pk=exclude_viewing.pk)
    return list(busy.values_list("scheduled_at", flat=True))


def open_slots(home, day: date, exclude_viewing=None) -> list[Slot]:
    first, last, length, notice = _config()
    zone = zone_for(home)
    day_start = datetime.combine(day, time(first), tzinfo=zone)
    day_end = datetime.combine(day, time(last), tzinfo=zone)
    earliest = timezone.now() + notice
    taken = _busy(home, day_start, day_end, exclude_viewing)

    slots, cursor = [], day_start
    while cursor + length <= day_end:
        clashes = any(abs(cursor - t) < length for t in taken)
        if cursor >= earliest and not clashes:
            slots.append(Slot(cursor))
        cursor += length
    return slots


def slot_at(home, day: date, hhmm: str, exclude_viewing=None) -> Slot | None:
    """The slot the caller picked, if it is still free - otherwise None."""
    return next((s for s in open_slots(home, day, exclude_viewing) if s.value == hhmm), None)
