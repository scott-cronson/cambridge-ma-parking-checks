from datetime import datetime

import pytest

from parking_checks import ET
from parking_checks.net import PoliteSession
from parking_checks.tow.client import (
    P,
    SiteFormatError,
    TowClient,
    normalize_plate,
    parse_hidden_fields,
    parse_makes,
    parse_results,
    parse_tow_time,
)
from tests.conftest import fixture_text


def test_parse_tow_time_truncates_to_minute():
    assert parse_tow_time("11/8/2026 7:27:26 PM") == datetime(2026, 11, 8, 19, 27, tzinfo=ET)
    assert parse_tow_time("10/30/2026 12:17 PM") == datetime(2026, 10, 30, 12, 17, tzinfo=ET)
    with pytest.raises(SiteFormatError):
        parse_tow_time("yesterday")


def test_normalize_plate():
    assert normalize_plate(" tes-t123 ") == "TEST123"


def test_table():
    makes = parse_makes(fixture_text("tow_form.html"))
    res = parse_results(fixture_text("tow_table.html"), makes)
    assert res.kind == "table"
    assert [(r.plate, r.make, r.model) for r in res.records] == [
        ("TST001", "TOYT", "4RUNNER"),
        ("TST002", "TOYT", "4RUNNER"),
    ]
    assert res.records[1].tow_datetime == datetime(2026, 11, 8, 19, 27, tzinfo=ET)
    assert res.records[0].detail_url.endswith("towedvehicles?plate=MA_TST001")
    assert "CAMRY" in res.models


def test_detail():
    res = parse_results(fixture_text("tow_detail.html"))
    assert res.kind == "detail" and len(res.records) == 1
    r = res.records[0]
    assert r.plate == "TST003" and r.tow_company == "B&B:PRIVATE" and r.is_detail
    assert r.tow_datetime == datetime(2026, 10, 31, 22, 15, tzinfo=ET)
    assert r.location.startswith("CAMBRIDGE")


def test_no_results():
    res = parse_results(fixture_text("tow_no_results.html"))
    assert res.kind == "none" and res.records == []


def test_missing_hidden_field():
    with pytest.raises(SiteFormatError, match="__VIEWSTATE"):
        parse_hidden_fields(fixture_text("tow_missing_hidden.html"))


def test_unrecognised_page():
    with pytest.raises(SiteFormatError):
        parse_results("<html><body><p>Maintenance</p></body></html>")
    with pytest.raises(SiteFormatError):
        parse_results('<div id="TowLookup"><p>Something new</p></div>')


class FakeResponse:
    def __init__(self, status: int, text: str):
        self.status_code = status
        self.text = text


class FakeSession:
    def __init__(self, posts: list[FakeResponse]):
        self.headers: dict[str, str] = {}
        self.posts = posts
        self.calls: list[tuple[str, dict | None]] = []

    def request(self, method, url, timeout=None, data=None, **kw):
        self.calls.append((method, data))
        if method == "GET":
            return FakeResponse(200, fixture_text("tow_form.html"))
        return self.posts.pop(0)


def client_with(posts):
    fake = FakeSession(posts)
    http = PoliteSession("test-agent", session=fake, sleep=lambda s: None)
    return TowClient(http), fake


def test_form_flow_posts_hidden_fields_and_skips_disabled_model():
    client, fake = client_with([FakeResponse(200, fixture_text("tow_no_results.html"))])
    res = client.search_plate("test123")
    assert res.kind == "none"
    method, data = fake.calls[-1]
    assert method == "POST"
    assert data[P + "txtPlateNum"] == "TEST123"
    assert data[P + "btnSearchTowLog"] == "Search"
    assert {"__VIEWSTATE", "__EVENTVALIDATION", "__VIEWSTATEGENERATOR"} <= data.keys()
    assert P + "ddlModel" not in data
    assert fake.headers["User-Agent"] == "test-agent"


def test_http_error_raises_with_status():
    client, _ = client_with([FakeResponse(500, "boom"), FakeResponse(500, "boom")])
    with pytest.raises(SiteFormatError) as ei:
        client.search_make("CHRYSLER")
    assert ei.value.status == 500
