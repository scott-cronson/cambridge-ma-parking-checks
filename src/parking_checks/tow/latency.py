"""Latency log: how long a tow takes to appear in the lookup.

The freshness check runs weekly, which is too coarse to time anything, so `tow-sweep` samples
every make every 3 hours for LATENCY_SAMPLE_DAYS after its first run, then stops by itself.
`tow-latency` reports first_seen_at minus tow_datetime for tows that first appeared after the
very first sampling run (earlier ones were already listed, so their delay is unknown).
"""

from __future__ import annotations

import statistics
from datetime import datetime, timedelta

from ..config import Config
from ..health import Outcome
from ..net import DataSourceError
from ..state import State
from .client import SiteFormatError, TowClient
from .plate_check import store

SWEEP = "tow-sweep"
SWEEP_INTERVAL_HOURS = 3
# Delays beyond this are treated as late (backdated) data entry rather than posting lag.
LATE_ENTRY_HOURS = 24


def run_sweep(cfg: Config, client: TowClient, state: State, now: datetime) -> Outcome:
    if cfg.latency_sample_days <= 0:
        return Outcome("skipped", "LATENCY_SAMPLE_DAYS=0")
    first = state.first_run(SWEEP)
    if first is not None:
        started = datetime.fromisoformat(first["started_at"])
        if now - started > timedelta(days=cfg.latency_sample_days):
            return Outcome("skipped", f"sampling period ended ({cfg.latency_sample_days} days)")
    makes = client.list_makes()
    new = seen = errors = unplated = 0
    for make in makes:
        try:
            res = client.search_make(make)
        except (SiteFormatError, DataSourceError):
            errors += 1
            continue
        if res.kind == "unplated":
            unplated += 1
            continue
        for rec in res.records:
            seen += 1
            new += store(state, rec, now, SWEEP)
    if errors and errors >= len(makes):
        raise DataSourceError(f"every make search failed ({errors})")
    return Outcome(
        "ok",
        f"{len(makes)} makes, {seen} tows listed, {new} new, {unplated} without a plate, {errors} errors",
    )


def latency_report(state: State) -> str:
    rows = state.tow_rows()
    if not rows:
        return "No tow records yet."
    earliest = min(datetime.fromisoformat(r["first_seen_at"]) for r in rows)
    baseline = earliest
    for name in (SWEEP, "tow-freshness", "tow-plate"):
        fr = state.first_run(name)
        if fr is None or not fr["finished_at"]:
            continue
        s, f = datetime.fromisoformat(fr["started_at"]), datetime.fromisoformat(fr["finished_at"])
        if s <= earliest <= f + timedelta(seconds=5):
            baseline = f
    delays: list[timedelta] = []
    for r in rows:
        seen = datetime.fromisoformat(r["first_seen_at"])
        towed = datetime.fromisoformat(r["tow_datetime"])
        if seen <= baseline + timedelta(seconds=5) or seen < towed:
            continue
        delays.append(seen - towed)
    if not delays:
        return (
            f"{len(rows)} tow records, but none first appeared after the first sampling run "
            f"({baseline:%Y-%m-%d %H:%M}). Let tow-sweep run for a few days."
        )
    hours = sorted(d.total_seconds() / 3600 for d in delays)
    prompt = [h for h in hours if h <= LATE_ENTRY_HOURS]
    late = [h for h in hours if h > LATE_ENTRY_HOURS]
    lines = [
        f"Tows timed: {len(hours)} (first appeared after {baseline:%Y-%m-%d %H:%M})",
        f"Resolution: up to {SWEEP_INTERVAL_HOURS} h (sweep interval), so true delays may be shorter.",
    ]
    if prompt:
        lines += [
            f"Posted within {LATE_ENTRY_HOURS} h: {len(prompt)} of {len(hours)}"
            f" (median {statistics.median(prompt):.1f} h, worst {prompt[-1]:.1f} h)",
        ]
    if late:
        lines.append(
            f"Late entries: {len(late)} of {len(hours)} listed {late[0]:.0f}-{late[-1]:.0f} h after "
            "their tow time (backdated data entry; polling more often cannot catch these sooner)"
        )
    if prompt and prompt[-1] > 12:
        lines.append("Prompt postings can take over 12 h: consider a second tow signal.")
    return "\n".join(lines)
