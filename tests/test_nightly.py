import json

import pytest

from parking_checks.cleaning.calendar import CalendarError, parse_events
from parking_checks.cleaning.nightly_check import RULE_LABEL, run_nightly_check
from tests.conftest import et, fixture_text


class FakeCalendar:
    """District C answers from the saved Recollect response; others from a dict."""

    def __init__(self, fail: bool = False):
        self.fail = fail
        self.calls = []
        self.c_dates = parse_events(json.loads(fixture_text("recollect_C.json")), "C")

    def cleaning_dates(self, district, after, before):
        self.calls.append((district, after, before))
        if self.fail:
            raise CalendarError("HTTP 503")
        return [c for c in self.c_dates if district == "C" and after <= c.date <= before]


def park(state, street="201 Franklin St", when="2026-12-02T18:12", device="driver1-iphone"):
    return state.save_location(
        received_at=et(when),
        captured_at=et(when),
        device=device,
        lat=42.36400,
        lon=-71.10300,
        street_raw=street,
        city="Cambridge",
        source="shortcut",
    )


def run(cfg, sched, state, notifier, clock, iso, calendar=None):
    clock.set(iso)
    return run_nightly_check(cfg, sched, state, notifier, calendar or FakeCalendar(), clock())


def test_heads_up_two_mornings_out(cfg, sched, state, notifier, clock, channel):
    park(state)
    out = run(cfg, sched, state, notifier, clock, "2026-12-02T21:00")
    assert out.status == "alerted"
    assert len(channel.sent) == 1
    subject, body, short = channel.sent[0]
    assert "Fri Dec 4" in subject and "heads-up" in subject
    assert body.startswith(
        "Street cleaning Fri Dec 4: odd-numbered side of Franklin St (district C). "
        "Car last logged at 201 Franklin St, Wed 6:12 pm, driver1-iphone."
    )
    assert RULE_LABEL not in body


def test_night_before_says_move(cfg, sched, state, notifier, clock, channel):
    park(state)
    run(cfg, sched, state, notifier, clock, "2026-12-03T21:00")
    assert len(channel.sent) == 1
    assert "Move it before 8:00 am Friday." in channel.sent[0][1]


def test_owner_hears_twice_then_dedupes(cfg, sched, state, notifier, clock, channel):
    park(state)
    run(cfg, sched, state, notifier, clock, "2026-12-02T21:00")
    run(cfg, sched, state, notifier, clock, "2026-12-02T21:05")  # rerun same night: no repeat
    run(cfg, sched, state, notifier, clock, "2026-12-03T21:00")
    assert len(channel.sent) == 2


def test_no_alert_after_cleaning(cfg, sched, state, notifier, clock, channel):
    park(state)
    out = run(cfg, sched, state, notifier, clock, "2026-12-05T21:00")
    assert out.status == "ok"
    assert channel.sent == []


def test_heads_up_even_side_monday(cfg, sched, state, notifier, clock, channel):
    park(state)
    run(cfg, sched, state, notifier, clock, "2026-12-12T21:00")
    assert len(channel.sent) == 1
    assert "Mon Dec 14" in channel.sent[0][0]
    assert "even-numbered side of Franklin St (district C)" in channel.sent[0][1]


def test_calendar_failure_falls_back_to_rules(cfg, sched, state, notifier, clock, channel):
    park(state)
    run(cfg, sched, state, notifier, clock, "2026-12-03T21:00", calendar=FakeCalendar(fail=True))
    subjects = [s for s, _, _ in channel.sent]
    cleaning = [b for s, b, _ in channel.sent if s.startswith("Street cleaning Fri")]
    assert len(cleaning) == 1 and RULE_LABEL in cleaning[0]
    assert "Street cleaning calendar unavailable" in subjects


def test_calendar_queried_with_three_day_window(cfg, sched, state, notifier, clock):
    park(state)
    cal = FakeCalendar()
    run(cfg, sched, state, notifier, clock, "2026-12-02T21:00", calendar=cal)
    assert [(d, str(a), str(b)) for d, a, b in cal.calls] == [("C", "2026-12-02", "2026-12-05")]


def test_no_location(cfg, sched, state, notifier, clock, channel):
    run(cfg, sched, state, notifier, clock, "2026-12-02T21:00")
    run(cfg, sched, state, notifier, clock, "2026-12-03T21:00")
    assert [s for s, _, _ in channel.sent] == ["No parking location on file"]


def test_schedule_unknown_once_per_parking_event(cfg, sched, state, notifier, clock, channel):
    park(state, street="12 Nowhere St")
    run(cfg, sched, state, notifier, clock, "2026-12-02T21:00")
    run(cfg, sched, state, notifier, clock, "2026-12-03T21:00")
    assert len(channel.sent) == 1 and "check the sign" in channel.sent[0][0]
    park(state, street="12 Nowhere St", when="2026-12-04T09:00")
    run(cfg, sched, state, notifier, clock, "2026-12-04T21:00")
    assert len(channel.sent) == 2


def test_outdated_location_flag(cfg, sched, state, notifier, clock, channel):
    park(state, when="2026-11-20T10:00")
    run(cfg, sched, state, notifier, clock, "2026-12-03T21:00")
    assert "location may be outdated" in channel.sent[0][1]


def test_off_season_exits_early(cfg, sched, state, notifier, clock, channel):
    park(state, when="2027-01-10T10:00")
    out = run(cfg, sched, state, notifier, clock, "2027-01-10T21:00")
    assert out.status == "skipped" and channel.sent == []


@pytest.mark.parametrize("street", ["100 Brookline St"])
def test_partial_unknown_still_alerts_known_side(cfg, sched, state, notifier, clock, channel, street):
    park(state, street=street)
    run(cfg, sched, state, notifier, clock, "2026-12-03T21:00")
    subjects = [s for s, _, _ in channel.sent]
    assert any(s.startswith("Schedule unknown") for s in subjects)
    assert any(s.startswith("Street cleaning Fri Dec 4") for s in subjects)
