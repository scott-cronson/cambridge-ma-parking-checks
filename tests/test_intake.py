from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from parking_checks.cleaning.intake import RateLimiter, create_app
from parking_checks.state import State
from tests.conftest import et


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return tmp_path / "state.db"


def make_client(cfg, sched, db, limiter=None, geocoder=None):
    app = create_app(
        cfg,
        lambda: State(db),
        sched,
        clock=lambda: et("2026-12-02T18:12"),
        limiter=limiter or RateLimiter(100, 60),
        geocoder=geocoder,
    )
    return TestClient(app)


BODY = {
    "lat": "42.36400",
    "lon": "-71.10300",
    "street": "201 Franklin St",
    "city": "Cambridge",
    "device": "driver1-iphone",
    "captured_at": "2026-12-02T18:12:03-05:00",
}


def auth(cfg):
    return {"Authorization": f"Bearer {cfg.intake_token}"}


def test_no_token_then_valid_token(cfg, sched, db):
    c = make_client(cfg, sched, db)
    r401 = c.post("/park", json=BODY)
    assert r401.status_code == 401 and r401.json()["message"] == "Not saved: unauthorized"
    assert c.post("/park", json=BODY, headers={"Authorization": "Bearer wrong"}).status_code == 401
    r = c.post("/park", json=BODY, headers=auth(cfg))
    assert r.status_code == 200, r.text
    assert (
        r.json()["message"]
        == "Saved: Franklin St. Next cleaning Fri Dec 4 (odd side), Mon Dec 14 (even side)."
    )
    last = c.get("/last", headers=auth(cfg))
    assert last.status_code == 200
    assert last.json()["street"] == "201 Franklin St"
    assert last.json()["device"] == "driver1-iphone"
    assert c.get("/last").status_code == 401


def test_implausible_coordinates(cfg, sched, db):
    c = make_client(cfg, sched, db)
    assert c.post("/park", json={**BODY, "lat": "91"}, headers=auth(cfg)).status_code == 422
    assert c.post("/park", json={**BODY, "lat": "0", "lon": "0"}, headers=auth(cfg)).status_code == 422
    assert c.post("/park", json={**BODY, "lat": "abc"}, headers=auth(cfg)).status_code == 422


def test_empty_street_uses_geocoder(cfg, sched, db):
    c = make_client(cfg, sched, db, geocoder=lambda lat, lon: ("201 Franklin Street", "Cambridge"))
    r = c.post("/park", json={**BODY, "street": ""}, headers=auth(cfg))
    assert r.status_code == 200
    assert c.get("/last", headers=auth(cfg)).json()["source"] == "shortcut+nominatim"


def test_rate_limit(cfg, sched, db):
    c = make_client(cfg, sched, db, limiter=RateLimiter(2, 60))
    codes = [c.post("/park", json=BODY, headers=auth(cfg)).status_code for _ in range(3)]
    assert codes == [200, 200, 429]


def test_bad_tokens_do_not_lock_out_phone(cfg, sched, db):
    c = make_client(cfg, sched, db, limiter=RateLimiter(2, 60))
    for _ in range(30):
        c.post("/park", json=BODY)
    assert c.post("/park", json=BODY, headers=auth(cfg)).status_code == 200


def test_requires_token_configured(cfg, sched, db):
    cfg.intake_token = "short"
    with pytest.raises(Exception, match="INTAKE_TOKEN"):
        make_client(cfg, sched, db)


def test_bad_body_explains_itself(cfg, sched, db):
    c = make_client(cfg, sched, db)
    r = c.post("/park", content=b"not json", headers={**auth(cfg), "Content-Type": "application/json"})
    assert r.status_code == 422 and r.json()["message"].startswith("Not saved:")
