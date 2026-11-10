from datetime import timedelta

from parking_checks.tow.client import SearchResult, SiteFormatError, TowRecord
from parking_checks.tow.freshness_check import order_makes, run_freshness_check


class Counter:
    request_count = 0


class FakeClient:
    def __init__(self, makes, results):
        self.makes = makes
        self.results = results  # make -> SearchResult | Exception
        self.searched: list[str] = []
        self.http = Counter()

    def list_makes(self):
        return self.makes

    def search_make(self, make):
        self.searched.append(make)
        self.http.request_count += 2
        r = self.results.get(make, SearchResult("none", [], []))
        if isinstance(r, Exception):
            raise r
        return r


def tows(*dts):
    return SearchResult("table", [TowRecord(plate=f"P{i}", tow_datetime=d) for i, d in enumerate(dts)], [])


MAKES = ["ACUR", "AUDI", "FORD", "HOND", "HONDA", "Not Available", "TOYT", "VOLV"]


def test_priority_order():
    assert order_makes(MAKES)[:5] == ["HOND", "HONDA", "TOYT", "FORD", "Not Available"]


def test_fresh_tow_stops_after_first_make(cfg, state, notifier, clock, channel):
    client = FakeClient(MAKES, {"HOND": tows(clock.now - timedelta(hours=3))})
    out = run_freshness_check(cfg, client, state, notifier, clock())
    assert out.status == "ok" and "site is live" in out.detail
    assert client.searched == ["HOND"]
    assert channel.sent == []


def test_all_old_is_stale_with_newest_date(cfg, state, notifier, clock, channel):
    newest = clock.now - timedelta(hours=30)
    client = FakeClient(MAKES, {"FORD": tows(newest), "VOLV": tows(clock.now - timedelta(days=5))})
    out = run_freshness_check(cfg, client, state, notifier, clock())
    assert out.status == "stale"
    assert sorted(client.searched) == sorted(MAKES)
    assert len(channel.sent) == 1
    subject, body, _ = channel.sent[0]
    assert subject == "Tow lookup data may be stale"
    assert "Mon Nov 9, 6:00 am" in body
    # Once per problem state per day.
    run_freshness_check(cfg, client, state, notifier, clock())
    assert len(channel.sent) == 1


def test_isolated_http_errors_are_tolerated(cfg, state, notifier, clock, channel):
    results = {"ACUR": SiteFormatError("HTTP 500", status=500), "HOND": tows(clock.now - timedelta(hours=1))}
    out = run_freshness_check(cfg, FakeClient(MAKES, results), state, notifier, clock())
    assert out.status == "ok"


def test_site_failure_is_separate_from_stale(cfg, state, notifier, clock, channel):
    results = {m: SiteFormatError("HTTP 503", status=503) for m in MAKES}
    out = run_freshness_check(cfg, FakeClient(MAKES, results), state, notifier, clock())
    assert out.status == "site_error"
    assert channel.sent[0][0] == "Tow lookup: site or format changed"


def test_format_change_is_reported_even_if_live(cfg, state, notifier, clock, channel):
    results = {"HOND": SiteFormatError("response has no #TowLookup block")}
    results["HONDA"] = tows(clock.now - timedelta(hours=1))
    out = run_freshness_check(cfg, FakeClient(MAKES, results), state, notifier, clock())
    assert out.status == "site_error"


def test_recovered_note(cfg, state, notifier, clock, channel):
    from parking_checks.health import run_check

    stale = FakeClient(MAKES, {})
    run_check(
        "tow-freshness",
        state,
        notifier,
        clock,
        lambda: run_freshness_check(cfg, stale, state, notifier, clock()),
    )
    clock.now += timedelta(days=7)
    live = FakeClient(MAKES, {"HOND": tows(clock.now - timedelta(hours=2))})
    run_check(
        "tow-freshness",
        state,
        notifier,
        clock,
        lambda: run_freshness_check(cfg, live, state, notifier, clock()),
    )
    assert [s for s, _, _ in channel.sent] == ["Tow lookup data may be stale", "Tow lookup recovered"]
