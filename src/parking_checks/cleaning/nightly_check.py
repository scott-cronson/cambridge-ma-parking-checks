"""2E. Nightly job (9:00 pm ET): alert when the parked block is swept on the next two mornings."""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta

from .. import ET
from ..config import Config
from ..fmt import day_label, full_label, when_label
from ..health import Outcome
from ..net import DataSourceError
from ..notify import Notifier
from ..state import Location, State
from .calendar import CityCalendar
from .matcher import (
    INFERRED,
    NO_STREET,
    OUTSIDE_COVERAGE,
    SCHEDULE_UNKNOWN,
    STREET_NOT_IN_FILE,
    Resolution,
    resolve,
    short_street,
)
from .schedule import SIDES, CleaningDate, Schedule, rule_dates

log = logging.getLogger(__name__)

OUTDATED_AFTER = timedelta(days=7)
RULE_LABEL = "computed from rule, not confirmed"
SIDE_TEXT = {"odd": "odd-numbered", "even": "even-numbered"}

Geocoder = Callable[[float, float], tuple[str | None, str | None]]


@dataclass
class DateLookup:
    dates: list[CleaningDate]
    errors: list[str]

    @property
    def calendar_failed(self) -> bool:
        return bool(self.errors)


def cleaning_dates_for(
    sched: Schedule,
    calendar: CityCalendar | None,
    districts: set[str],
    after: date,
    before: date,
) -> DateLookup:
    """Dates strictly between after and before. City calendar first, rules as fallback.

    When the calendar answers, a plain (non-holiday) rule date it does not list is still
    returned, labelled, because over-alerting is preferred to a missed cleaning.
    """
    out: list[CleaningDate] = []
    errors: list[str] = []
    for district in sorted(districts):
        rules = [
            c
            for side in SIDES
            for c in rule_dates(sched, district, side, after + timedelta(days=1), before - timedelta(days=1))
        ]
        if calendar is None:
            out += rules
            errors.append(f"district {district}: no calendar configured")
            continue
        try:
            cal = [c for c in calendar.cleaning_dates(district, after, before) if after < c.date < before]
        except DataSourceError as e:
            log.warning("calendar failed for %s: %s", district, e)
            errors.append(f"district {district}: {e}")
            out += rules
            continue
        listed = {(c.date, c.side) for c in cal}
        out += cal
        for r in rules:
            if not r.holiday_conflict and (r.date, r.side) not in listed:
                out.append(replace(r, note="rule date not listed by the city calendar; check the sign"))
    return DateLookup(out, errors)


def _logged_label(loc: Location, now: datetime) -> str:
    """'Wed 6:12 pm' within the last 6 days, else 'Wed Nov 25, 6:12 pm'."""
    if now - loc.logged_at < timedelta(days=6):
        return when_label(loc.logged_at)
    return f"{loc.logged_at.astimezone(ET):%a} {full_label(loc.logged_at).split(' ', 1)[1]}"


def _flags_text(res: Resolution, loc: Location, now: datetime) -> list[str]:
    notes = []
    if INFERRED in res.flags:
        notes.append("schedule inferred from nearby addresses")
    if SCHEDULE_UNKNOWN in res.flags and res.pairs:
        sides = " and ".join(sorted(res.unknown_sides)) or "some"
        notes.append(f"the {sides} side has no schedule in the file here; check the sign")
    if now - loc.logged_at > OUTDATED_AFTER:
        notes.append("location may be outdated")
    return notes


def _unknown_note(res: Resolution, loc: Location, now: datetime) -> tuple[str, str, str]:
    reason = {
        OUTSIDE_COVERAGE: "is outside the area the schedule file covers",
        STREET_NOT_IN_FILE: "is on a street the schedule file does not list",
        NO_STREET: "has no street name (the phone did not resolve one)",
        SCHEDULE_UNKNOWN: "is on a block the city tool returned no schedule for",
    }
    why = next(
        (
            reason[f]
            for f in (OUTSIDE_COVERAGE, STREET_NOT_IN_FILE, NO_STREET, SCHEDULE_UNKNOWN)
            if f in res.flags
        ),
        "",
    )
    subject = f"Schedule unknown at {res.display}: check the sign"
    known = ", ".join(f"district {d} {s} side" for d, s in sorted(res.pairs))
    body = (
        f"The car was last logged at {res.display} ({_logged_label(loc, now)}, {loc.device}), "
        f"which {why}.\n\n"
        + (f"Alerts will still be sent for the known part of this street: {known}.\n\n" if res.pairs else "")
        + "Check the posted street-cleaning sign for this block."
    )
    short = f"Parked at {res.display}: cleaning schedule unknown here, check the sign."
    return subject, body, short


def _conflict_message(
    when: date,
    items: list[CleaningDate],
    res: Resolution,
    loc: Location,
    today: date,
    notes: list[str],
    move_by: str,
    now: datetime,
) -> tuple[str, str, str, str]:
    stage = "final" if (when - today).days <= 1 else "headsup"
    street = short_street(res.street or res.display)
    pairs = sorted({(c.district, c.side) for c in items}, key=lambda p: (p[1] != "odd", p[0]))
    if len({d for d, _ in pairs}) == 1 and len(pairs) == 1:
        where = f"{SIDE_TEXT[pairs[0][1]]} side of {street} (district {pairs[0][0]})"
    else:
        where = " and ".join(f"{SIDE_TEXT[s]} side (district {d})" for d, s in pairs) + f" of {street}"
    labels = list(notes)
    if any(c.source != "city_calendar" for c in items):
        labels.insert(0, RULE_LABEL)
    for c in items:
        if c.holiday_conflict and c.source == "rule":
            labels.append(c.note)
        elif c.note and c.source != "city_calendar":
            labels.append(c.note)
    labels = list(dict.fromkeys(x for x in labels if x))
    wd = f"{when:%A}"
    logged = f"Car last logged at {res.display}, {_logged_label(loc, now)}, {loc.device}."
    if stage == "final":
        action = f"Move it before {move_by} {wd}."
    else:
        action = f"Heads-up: move it before {move_by} {wd}. You will get a reminder tomorrow night."
    first = f"Street cleaning {day_label(when)}: {where}."
    body = f"{first} {logged} {action}"
    if labels:
        body += "\n\nNotes: " + "; ".join(labels) + "."
    subject = f"Street cleaning {day_label(when)}: {street}" + (
        " (heads-up)" if stage == "headsup" else ", move the car"
    )
    short = f"{first} {action}"
    return stage, subject, body, short


def run_nightly_check(
    cfg: Config,
    sched: Schedule,
    state: State,
    notifier: Notifier,
    calendar: CityCalendar | None,
    now: datetime,
    geocoder: Geocoder | None = None,
) -> Outcome:
    today = now.astimezone(ET).date()
    targets = [today + timedelta(days=i) for i in range(1, cfg.cleaning_lookahead_days + 1)]
    if not any(sched.in_season(t) for t in targets):
        return Outcome("skipped", "no street cleaning January to March")

    loc = state.latest_location()
    if loc is None:
        notifier.notify(
            "no-location",
            "No parking location on file",
            "The nightly street-cleaning check has no parked location yet. Run the "
            "'Log Parking Spot' Shortcut on a phone (or check its Bluetooth automation).",
            "no-location",
        )
        return Outcome("ok", "no location on file")

    street_raw = loc.street_raw
    if not street_raw and loc.lat is not None and loc.lon is not None and geocoder:
        street, city = geocoder(loc.lat, loc.lon)
        if street:
            state.update_location_street(loc.id, street, f"{loc.source}+nominatim")
            street_raw = street
            loc.city = loc.city or city
    res = resolve(sched, street_raw, loc.lat, loc.lon, loc.city)

    if res.schedule_unknown:
        subject, body, short = _unknown_note(res, loc, now)
        notifier.notify("schedule-unknown", subject, body, f"unknown:{loc.id}", short=short)
        if not res.pairs:
            return Outcome("ok", f"schedule unknown at {res.display}: {sorted(res.flags)}")

    lookup = cleaning_dates_for(
        sched,
        calendar,
        {d for d, _ in res.pairs},
        today,
        today + timedelta(days=cfg.cleaning_lookahead_days + 1),
    )
    if lookup.calendar_failed and calendar is not None:
        notifier.notify(
            "site-changed",
            "Street cleaning calendar unavailable",
            "The city's street-cleaning calendar could not be read, so tonight's check used the "
            "nth-weekday rules instead (holiday moves are not known).\n\n" + "\n".join(lookup.errors),
            f"calendar-failed:{today.isoformat()}",
            short="Street-cleaning calendar unavailable; using rule dates tonight.",
        )

    by_date: dict[date, list[CleaningDate]] = defaultdict(list)
    for c in lookup.dates:
        if c.date in targets and (c.district, c.side) in res.pairs:
            by_date[c.date].append(c)

    notes = _flags_text(res, loc, now)
    move_by = "8:00 am"
    sent = []
    for when in sorted(by_date):
        stage, subject, body, short = _conflict_message(
            when, by_date[when], res, loc, today, notes, move_by, now
        )
        notifier.notify(
            "street-cleaning", subject, body, f"cleaning:{loc.id}:{when.isoformat()}:{stage}", short=short
        )
        sent.append(f"{when.isoformat()} {stage}")
    pairs = ",".join(f"{d}-{s}" for d, s in sorted(res.pairs))
    src = "rules" if lookup.calendar_failed else "city calendar"
    if sent:
        return Outcome("alerted", f"{res.display} [{pairs}] via {src}: {'; '.join(sent)}")
    return Outcome("ok", f"{res.display} [{pairs}] via {src}: no cleaning {targets[0]}..{targets[-1]}")
