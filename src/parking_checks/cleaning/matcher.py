"""2D. Match a parked location to schedule segments.

resolve() takes both sides of the street (the side is not known) and deliberately
over-alerts near district boundaries: in a gap between segments or beyond the ends of a side,
every adjacent segment applies.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any

from .schedule import SIDES, Schedule

ABBREV = {
    "st": "Street",
    "str": "Street",
    "ave": "Avenue",
    "av": "Avenue",
    "pl": "Place",
    "ct": "Court",
    "dr": "Drive",
    "ter": "Terrace",
    "terr": "Terrace",
    "ext": "Extension",
    "rd": "Road",
    "sq": "Square",
    "s": "South",
    "n": "North",
}
COVERAGE_RADIUS_M = 600

STREET_NOT_IN_FILE = "street_not_in_file"
OUTSIDE_COVERAGE = "outside_coverage"
SCHEDULE_UNKNOWN = "schedule_unknown"
INFERRED = "inferred"
NO_STREET = "no_street"
UNKNOWN_FLAGS = {STREET_NOT_IN_FILE, OUTSIDE_COVERAGE, SCHEDULE_UNKNOWN, NO_STREET}


@dataclass
class Resolution:
    street_raw: str | None
    street: str | None  # name as in the schedule file
    display: str  # e.g. "Franklin St", for messages
    house_number: int | None
    pairs: set[tuple[str, str]] = field(default_factory=set)  # (district, side)
    flags: set[str] = field(default_factory=set)
    unknown_sides: set[str] = field(default_factory=set)

    @property
    def schedule_unknown(self) -> bool:
        return bool(self.flags & UNKNOWN_FLAGS)


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


_NUM_RE = re.compile(r"^\s*(\d+)(?:\s*[-–]\s*\d+)?[A-Za-z]?\b[\s,]*(.*)$")


def split_address(raw: str) -> tuple[int | None, str]:
    """'201 Franklin St' -> (201, 'Franklin St'); '201-203 Franklin St' -> (201, ...)."""
    raw = " ".join((raw or "").replace(",", " ").split())
    m = _NUM_RE.match(raw)
    if m:
        return int(m.group(1)), m.group(2).strip()
    return None, raw


def normalize_street(name: str) -> str:
    words = [w.strip(".") for w in name.split()]
    out = []
    for i, w in enumerate(words):
        key = w.lower()
        # Only expand a leading S/N as a direction; "S" elsewhere is unlikely but keep it.
        if key in ("s", "n") and i != 0:
            out.append(w)
            continue
        out.append(ABBREV.get(key, w[:1].upper() + w[1:].lower() if w else w))
    return " ".join(out)


def _lookup(sched: Schedule, name: str) -> str | None:
    by_lower = {k.lower(): k for k in sched.streets}
    norm = normalize_street(name)
    aliases = {k.lower(): v for k, v in sched.aliases.items()}
    for candidate in (norm, aliases.get(norm.lower(), ""), aliases.get(name.lower(), "")):
        if candidate and candidate.lower() in by_lower:
            return by_lower[candidate.lower()]
    return None


def _segments_for(segments: list[dict[str, Any]], n: int | None) -> list[dict[str, Any]]:
    if not segments:
        return []
    segs = sorted(segments, key=lambda s: (s["from"], s["to"]))
    if n is None:
        return segs
    inside = [s for s in segs if s["from"] <= n <= s["to"]]
    if inside:
        return inside
    below = [s for s in segs if s["to"] < n]
    above = [s for s in segs if s["from"] > n]
    picked = []
    if below:
        picked.append(max(below, key=lambda s: s["to"]))
    if above:
        picked.append(min(above, key=lambda s: s["from"]))
    return picked


def resolve(
    sched: Schedule,
    street_raw: str | None,
    lat: float | None = None,
    lon: float | None = None,
    city: str | None = None,
) -> Resolution:
    number, name = split_address(street_raw or "")
    display = " ".join(x for x in (str(number) if number else "", name) if x) or "unknown street"
    res = Resolution(street_raw, None, display, number)

    def far() -> bool:
        if lat is None or lon is None or sched.center is None:
            return False
        clat, clon = sched.center
        return haversine_m(lat, lon, clat, clon) > COVERAGE_RADIUS_M

    if city and city.strip().lower() not in ("cambridge", "cambridge, ma"):
        res.flags.add(OUTSIDE_COVERAGE)
        return res
    if not name:
        res.flags.add(OUTSIDE_COVERAGE if far() else NO_STREET)
        return res
    street = _lookup(sched, name)
    if street is None:
        res.flags.add(OUTSIDE_COVERAGE if far() else STREET_NOT_IN_FILE)
        return res
    res.street = street
    entry = sched.streets[street]
    for side in SIDES:
        for seg in _segments_for(entry.get(f"{side}_side", []), number):
            if seg.get("source") == "inferred_from_nearby_addresses":
                res.flags.add(INFERRED)
            if not seg.get("district") or not seg.get("schedule"):
                res.flags.add(SCHEDULE_UNKNOWN)
                res.unknown_sides.add(side)
                continue
            res.pairs.add((seg["district"], side))
    if not res.pairs and not res.flags:
        res.flags.add(SCHEDULE_UNKNOWN)
    return res


def short_street(display_or_name: str) -> str:
    """'Franklin Street' -> 'Franklin St' for compact messages."""
    rev = {"Street": "St", "Avenue": "Ave", "Place": "Pl", "Court": "Ct", "Drive": "Dr", "Terrace": "Ter"}
    return " ".join(rev.get(w, w) for w in display_or_name.split())
