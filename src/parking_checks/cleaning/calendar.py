"""City street-cleaning calendar (Recollect service 762, unofficial).

Response shape confirmed Nov 2026: {"events": [{"day": "YYYY-MM-DD", "flags": [{"name":
"street_sweeping_odd" | "street_sweeping_even" | "holiday", ...}], ...}], "zones": ..., ...}.
"""

from __future__ import annotations

from datetime import date

from ..net import DataSourceError, PoliteSession
from .schedule import CleaningDate, Schedule

FLAG_SIDE = {"street_sweeping_odd": "odd", "street_sweeping_even": "even"}


class CalendarError(DataSourceError):
    pass


def parse_events(payload: object, district: str) -> list[CleaningDate]:
    if not isinstance(payload, dict) or not isinstance(payload.get("events"), list):
        raise CalendarError("calendar response has no events list")
    out: list[CleaningDate] = []
    for ev in payload["events"]:
        if not isinstance(ev, dict) or "day" not in ev or not isinstance(ev.get("flags", []), list):
            raise CalendarError("calendar event missing day or flags")
        names = {f.get("name") for f in ev.get("flags", []) if isinstance(f, dict)}
        names.add(ev.get("type"))
        for flag, side in FLAG_SIDE.items():
            if flag in names:
                try:
                    d = date.fromisoformat(ev["day"])
                except (TypeError, ValueError) as e:
                    raise CalendarError(f"bad event day {ev.get('day')!r}") from e
                out.append(CleaningDate(d, district, side, "city_calendar"))
    return out


class CityCalendar:
    def __init__(self, http: PoliteSession, sched: Schedule):
        self.http = http
        self.sched = sched
        self.template = sched.data["city_calendar_lookup"]["url_template"]

    def url(self, place_id: str, after: date, before: date) -> str:
        # The template has two {YYYY-MM-DD} placeholders: after, then before.
        first, _, rest = self.template.partition("{YYYY-MM-DD}")
        second, _, tail = rest.partition("{YYYY-MM-DD}")
        return (first + after.isoformat() + second + before.isoformat() + tail).replace(
            "{place_id}", place_id
        )

    def cleaning_dates(self, district: str, after: date, before: date) -> list[CleaningDate]:
        place = self.sched.reference_place(district)
        if not place:
            raise CalendarError(f"no reference place for district {district}")
        r = self.http.get(self.url(place, after, before))
        if r.status_code != 200:
            raise CalendarError(f"calendar district {district}: HTTP {r.status_code}")
        try:
            payload = r.json()
        except ValueError as e:
            raise CalendarError(f"calendar district {district}: not JSON") from e
        return parse_events(payload, district)
