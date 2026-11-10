from datetime import timedelta

from parking_checks.tow.client import parse_results
from parking_checks.tow.plate_check import run_plate_check
from tests.conftest import fixture_text


class FakeTowClient:
    def __init__(self, html: str, detail_html: str | None = None):
        self.html = html
        self.detail_html = detail_html
        self.detail_calls = 0

    def search_plate(self, text):
        return parse_results(self.html)

    def get_detail(self, url):
        self.detail_calls += 1
        return parse_results(self.detail_html).records[0] if self.detail_html else None


def detail_for_plate(plate="TEST123"):
    return fixture_text("tow_detail.html").replace("TST003", plate)


def test_hit_alerts_once_then_every_12h_up_to_max(cfg, state, notifier, clock, channel):
    client = FakeTowClient(detail_for_plate())
    run_plate_check(cfg, client, state, notifier, clock())
    run_plate_check(cfg, client, state, notifier, clock())  # same run window: no repeat
    assert len(channel.sent) == 1
    subject, body, short = channel.sent[0]
    assert subject.startswith("CAR TOWED: TEST123 towed Sat Oct 31, 10:15 pm")
    assert "617-349-3300" in body and "617-492-5781" in body
    assert "towedvehicles" in body

    for _ in range(5):
        clock.now += timedelta(hours=12)
        run_plate_check(cfg, client, state, notifier, clock())
    # 1 initial + TOW_REMINDER_MAX (4) reminders, then quiet.
    assert len(channel.sent) == 1 + cfg.tow_reminder_max
    assert "reminder 4 of 4" in channel.sent[-1][0]
    assert len(state.tow_rows()) == 1


def test_partial_match_does_not_alert(cfg, state, notifier, clock, channel):
    client = FakeTowClient(detail_for_plate("TEST1234"))
    out = run_plate_check(cfg, client, state, notifier, clock())
    assert out.status == "ok" and channel.sent == []
    assert state.tow_rows()[0]["plate"] == "TEST1234"  # still logged for latency


def test_table_hit_fetches_detail(cfg, state, notifier, clock, channel):
    table = fixture_text("tow_table.html").replace(">TST001<", ">TEST123<")
    detail = fixture_text("tow_detail.html").replace("TST003", "TEST123")
    client = FakeTowClient(table, detail)
    run_plate_check(cfg, client, state, notifier, clock())
    assert client.detail_calls == 1
    assert len(channel.sent) == 1
    assert "B&B:PRIVATE" in channel.sent[0][1]


def test_no_results(cfg, state, notifier, clock, channel):
    out = run_plate_check(cfg, FakeTowClient(fixture_text("tow_no_results.html")), state, notifier, clock())
    assert out.status == "ok" and channel.sent == []
