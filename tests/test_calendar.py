import json
from datetime import date

import pytest

from parking_checks.cleaning.calendar import CalendarError, CityCalendar, parse_events
from parking_checks.net import PoliteSession
from tests.conftest import fixture_text


def test_parse_saved_response():
    got = parse_events(json.loads(fixture_text("recollect_C.json")), "C")
    assert {(c.date, c.side) for c in got} == {(date(2026, 12, 4), "odd"), (date(2026, 12, 14), "even")}
    assert all(c.source == "city_calendar" for c in got)


@pytest.mark.parametrize("payload", [[], {"nope": 1}, {"events": [{"flags": []}]}])
def test_bad_shape_raises(payload):
    with pytest.raises(CalendarError):
        parse_events(payload, "C")


def test_url(sched):
    cal = CityCalendar(PoliteSession("t"), sched)
    url = cal.url("PLACE", date(2026, 12, 2), date(2026, 12, 5))
    assert url.startswith("https://api.recollect.net/api/places/PLACE/services/762/events?")
    assert "after=2026-12-02&before=2026-12-05" in url
