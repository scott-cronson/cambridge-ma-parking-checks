"""2A. HTTPS intake for parked locations (behind Tailscale Funnel on :8443).

POST /park  Authorization: Bearer <INTAKE_TOKEN>, JSON {lat, lon, street, city, device, captured_at}
GET  /last  same token; newest stored row, for testing
GET  /healthz  no token; "ok"
"""

from __future__ import annotations

import hmac
import logging
import threading
import time
from collections import deque
from collections.abc import Callable
from datetime import datetime, timedelta

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .. import ET
from ..config import Config
from ..fmt import day_label
from ..state import Location, State
from .matcher import resolve, short_street
from .nightly_check import Geocoder
from .schedule import Schedule, next_rule_dates

log = logging.getLogger(__name__)

RATE_LIMIT = 6  # authorised requests per window
RATE_WINDOW_S = 60.0
BAD_AUTH_LIMIT = 20


class ParkIn(BaseModel):
    lat: str | float | None = None
    lon: str | float | None = None
    street: str | None = Field(default=None, max_length=200)
    city: str | None = Field(default=None, max_length=100)
    device: str = Field(default="unknown", max_length=60)
    captured_at: str | None = Field(default=None, max_length=60)


class RateLimiter:
    def __init__(self, limit: int, window_s: float, monotonic: Callable[[], float] = time.monotonic):
        self.limit, self.window, self.monotonic = limit, window_s, monotonic
        self.hits: deque[float] = deque()
        self.lock = threading.Lock()

    def allow(self) -> bool:
        with self.lock:
            now = self.monotonic()
            while self.hits and now - self.hits[0] > self.window:
                self.hits.popleft()
            if len(self.hits) >= self.limit:
                return False
            self.hits.append(now)
            return True


def _coord(v: str | float | None, lo: float, hi: float, name: str) -> float | None:
    if v is None or (isinstance(v, str) and not v.strip()):
        return None
    try:
        f = float(str(v).strip().replace(",", "."))
    except ValueError as e:
        raise HTTPException(422, f"{name} is not a number") from e
    if not lo <= f <= hi:
        raise HTTPException(422, f"{name} out of range")
    return f


def _parse_captured(v: str | None) -> datetime | None:
    if not v:
        return None
    try:
        dt = datetime.fromisoformat(v.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=ET)


def reply_message(sched: Schedule, loc: Location, now: datetime) -> str:
    res = resolve(sched, loc.street_raw, loc.lat, loc.lon, loc.city)
    name = short_street(res.street) if res.street else res.display
    head = f"Saved: {name}."
    if res.schedule_unknown and not res.pairs:
        return f"{head} Schedule unknown here, check the sign."
    local = now.astimezone(ET)
    start = local.date() if local.hour < 8 else local.date() + timedelta(days=1)
    multi = len({d for d, _ in res.pairs}) > 1
    upcoming = []
    for district, side in sorted(res.pairs):
        dates = next_rule_dates(sched, district, side, start)
        if not dates:
            continue
        c = dates[0]
        label = f"{side} side" + (f", district {district}" if multi else "")
        extra = " (holiday, may move)" if c.holiday_conflict and c.source == "rule" else ""
        upcoming.append((c.date, f"{day_label(c.date)} ({label}){extra}"))
    if not upcoming:
        return f"{head} No cleaning until April."
    msg = f"{head} Next cleaning " + ", ".join(t for _, t in sorted(upcoming)) + "."
    if res.schedule_unknown:
        msg += " Part of this street has no schedule; check the sign."
    return msg


def create_app(
    cfg: Config,
    state_factory: Callable[[], State],
    sched: Schedule,
    *,
    clock: Callable[[], datetime] = lambda: datetime.now(ET),
    geocoder: Geocoder | None = None,
    limiter: RateLimiter | None = None,
) -> FastAPI:
    cfg.require_intake_token()
    app = FastAPI(title="parking-checks intake", docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def log_requests(request: Request, call_next):
        # One line per request for troubleshooting the Shortcut. Never logs headers or the token.
        response = await call_next(request)
        ua = (request.headers.get("user-agent") or "-")[:40]
        log.info("%s %s -> %s (ua=%s)", request.method, request.url.path, response.status_code, ua)
        return response

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        log.warning("rejected %s: %s %s", request.url.path, exc.status_code, exc.detail)
        return JSONResponse({"detail": exc.detail, "message": f"Not saved: {exc.detail}"}, exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        problems = [f"{'.'.join(str(x) for x in e.get('loc', ()))}: {e.get('msg')}" for e in exc.errors()]
        log.warning("rejected %s body: %s", request.url.path, "; ".join(problems))
        return JSONResponse({"detail": problems, "message": "Not saved: " + "; ".join(problems)}, 422)

    limiter = limiter or RateLimiter(RATE_LIMIT, RATE_WINDOW_S)
    # Bad tokens get their own budget, so junk traffic from the public URL cannot lock out
    # the phones.
    bad_limiter = RateLimiter(BAD_AUTH_LIMIT, RATE_WINDOW_S)
    expected = f"Bearer {cfg.intake_token}".encode()

    def guard(authorization: str | None = Header(default=None)) -> None:
        if not authorization or not hmac.compare_digest(authorization.encode(), expected):
            if not bad_limiter.allow():
                raise HTTPException(429, "rate limited")
            raise HTTPException(401, "unauthorized")
        if not limiter.allow():
            raise HTTPException(429, "rate limited")

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/park", dependencies=[Depends(guard)])
    def park(body: ParkIn) -> dict[str, object]:
        lat = _coord(body.lat, -90, 90, "lat")
        lon = _coord(body.lon, -180, 180, "lon")
        if (lat is None) != (lon is None) or (lat == 0 and lon == 0):
            raise HTTPException(422, "implausible coordinates")
        street = (body.street or "").strip() or None
        city = (body.city or "").strip() or None
        source = "shortcut"
        if street is None and lat is not None and geocoder is not None:
            street, gcity = geocoder(lat, lon)  # type: ignore[arg-type]
            city = city or gcity
            source = "shortcut+nominatim" if street else source
        if street is None and lat is None:
            raise HTTPException(422, "need coordinates or a street")
        now = clock()
        state = state_factory()
        try:
            loc = state.save_location(
                received_at=now,
                captured_at=_parse_captured(body.captured_at),
                device=body.device.strip() or "unknown",
                lat=lat,
                lon=lon,
                street_raw=street,
                city=city,
                source=source,
            )
        finally:
            state.close()
        msg = reply_message(sched, loc, now)
        log.info("saved location %s from %s: %r (%s)", loc.id, loc.device, loc.street_raw, source)
        return {"ok": True, "id": loc.id, "message": msg}

    @app.get("/last", dependencies=[Depends(guard)])
    def last() -> dict[str, object]:
        state = state_factory()
        try:
            loc = state.latest_location()
        finally:
            state.close()
        if loc is None:
            raise HTTPException(404, "no location stored")
        return {
            "id": loc.id,
            "received_at": loc.received_at.isoformat(),
            "captured_at": loc.captured_at.isoformat() if loc.captured_at else None,
            "device": loc.device,
            "lat": loc.lat,
            "lon": loc.lon,
            "street": loc.street_raw,
            "city": loc.city,
            "source": loc.source,
        }

    return app
