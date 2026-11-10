"""Cleaning dates per district and side.

Primary source: the city's calendar (Recollect, see calendar.py), which includes holiday moves.
Fallback: the nth-weekday rules in the schedule file, April to December, labelled as computed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

SIDES = ("odd", "even")


@dataclass(frozen=True)
class CleaningDate:
    date: date
    district: str
    side: str  # odd | even
    source: str  # city_calendar | rule | rule_relocated
    holiday_conflict: bool = False
    rule_date: date | None = None
    note: str = ""


@dataclass
class Schedule:
    data: dict[str, Any]
    path: Path | None = None
    _holidays: dict[date, str] = field(default_factory=dict)

    center_point: tuple[float, float] | None = None

    @classmethod
    def load(cls, path: Path, center: tuple[float, float] | None = None) -> Schedule:
        """center is the coverage point (COVERAGE_CENTER_LAT/LON); it is kept out of the file."""
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if data.get("meta", {}).get("schema_version", 0) < 4:
            raise ValueError("schedule file schema_version must be >= 4")
        s = cls(data, Path(path))
        s.center_point = center
        for items in data.get("holidays", {}).values():
            for h in items:
                for k in ("date", "observed_date"):
                    if h.get(k):
                        s._holidays[date.fromisoformat(h[k])] = h["name"]
        return s

    @property
    def meta(self) -> dict[str, Any]:
        return self.data["meta"]

    @property
    def streets(self) -> dict[str, Any]:
        return self.data["streets"]

    @property
    def aliases(self) -> dict[str, str]:
        return {k: v for k, v in self.data.get("street_aliases", {}).items() if not k.startswith("_")}

    @property
    def center(self) -> tuple[float, float] | None:
        return self.center_point

    @property
    def season(self) -> tuple[int, int]:
        s = self.meta["season"]
        return int(s["start_month"]), int(s["end_month"])

    def in_season(self, d: date) -> bool:
        a, b = self.season
        return a <= d.month <= b

    def district_rule(self, district: str, side: str) -> dict[str, Any]:
        return self.data["districts"][district][f"{side}_side"]

    def holiday_name(self, d: date) -> str | None:
        return self._holidays.get(d)

    def known_conflict(self, district: str, side: str, rule_date: date) -> dict[str, Any] | None:
        conflicts = self.data["districts"][district].get("holiday_conflicts", {}).get(side, [])
        for c in conflicts:
            if c.get("rule_date") == rule_date.isoformat():
                return c
        return None

    def reference_place(self, district: str) -> str | None:
        ref = self.data.get("city_calendar_lookup", {}).get("reference_places", {}).get(district)
        return ref.get("place_id") if ref else None


def nth_weekday(year: int, month: int, weekday_iso: int, nth: int) -> date:
    d = date(year, month, 1)
    offset = (weekday_iso - d.isoweekday()) % 7
    return d + timedelta(days=offset + 7 * (nth - 1))


def _months(start: date, end: date):
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        yield y, m
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)


@dataclass(frozen=True)
class MonthRule:
    rule_date: date
    holiday: str | None  # name when the rule date is a city holiday
    actual_date: date | None  # confirmed relocated date, when the file records one

    @property
    def holiday_conflict(self) -> bool:
        return self.holiday is not None


def month_rule(sched: Schedule, district: str, side: str, year: int, month: int) -> MonthRule | None:
    """The rule date for one month, with holiday information. None outside the season."""
    if not sched.in_season(date(year, month, 1)):
        return None
    rule = sched.district_rule(district, side)
    rd = nth_weekday(year, month, int(rule["weekday_iso"]), int(rule["nth"]))
    conflict = sched.known_conflict(district, side, rd)
    holiday = sched.holiday_name(rd) or (conflict or {}).get("holiday")
    actual = conflict.get("actual_date") if conflict else None
    return MonthRule(rd, holiday, date.fromisoformat(actual) if actual else None)


def rule_dates(sched: Schedule, district: str, side: str, start: date, end: date) -> list[CleaningDate]:
    """Cleaning dates computed from the rule for start <= date <= end.

    A rule date that lands on a holiday is a holiday conflict. If the file records the
    confirmed relocated date, that date is returned (source rule_relocated); otherwise the
    rule date is returned flagged holiday_conflict, since the city moves it to a date that
    cannot be computed.
    """
    out: list[CleaningDate] = []
    # Relocations move a cleaning days to ~2 weeks, so look one month either side.
    for y, m in _months(start - timedelta(days=31), end + timedelta(days=31)):
        mr = month_rule(sched, district, side, y, m)
        if mr is None:
            continue
        if mr.actual_date is not None:
            if start <= mr.actual_date <= end:
                out.append(
                    CleaningDate(
                        mr.actual_date,
                        district,
                        side,
                        "rule_relocated",
                        holiday_conflict=True,
                        rule_date=mr.rule_date,
                        note=f"moved from {mr.rule_date.isoformat()} ({mr.holiday})",
                    )
                )
            continue
        if start <= mr.rule_date <= end:
            out.append(
                CleaningDate(
                    mr.rule_date,
                    district,
                    side,
                    "rule",
                    holiday_conflict=mr.holiday_conflict,
                    rule_date=mr.rule_date,
                    note=(
                        f"{mr.holiday}: the city usually moves this cleaning; check the sign"
                        if mr.holiday
                        else ""
                    ),
                )
            )
    return sorted(out, key=lambda c: c.date)


def next_rule_dates(
    sched: Schedule, district: str, side: str, today: date, horizon_days: int = 120
) -> list[CleaningDate]:
    return rule_dates(sched, district, side, today, today + timedelta(days=horizon_days))
