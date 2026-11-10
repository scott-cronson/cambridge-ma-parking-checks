"""1B. Freshness check: prove the lookup is live and still receiving new tows.

Step 0 showed that a make-only search returns every tow for that make, so the scan works per
make (about 40 makes) rather than per make and model. Makes are searched most-towed first and
the scan stops at the first tow younger than FRESHNESS_MAX_AGE_HOURS.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from ..config import Config
from ..fmt import full_label
from ..health import Outcome
from ..net import DataSourceError
from ..notify import Notifier
from ..state import State
from .client import LOOKUP_URL, SiteFormatError, TowClient
from .plate_check import store

log = logging.getLogger(__name__)

CHECK = "tow-freshness"
MAX_REQUESTS = 200

# Canonical make, then spellings seen in the dropdown. Most-towed first.
PRIORITY: list[tuple[str, tuple[str, ...]]] = [
    ("HONDA", ("HOND", "HONDA", "HOD")),
    ("TOYOTA", ("TOYT", "TOYOTA", "TOT", "TOYO")),
    ("FORD", ("FORD",)),
    ("NISSAN", ("NISS", "NISSAN", "NISN")),
    ("SUBARU", ("SUBARU", "SUBA", "SUB")),
    ("HYUNDAI", ("HYUN", "HYUNDAI", "HYUND")),
    ("CHEVROLET", ("CHEV", "CHEVY", "CHEVROLET")),
    ("UNKNOWN", ("NOT AVAILABLE",)),
]


def order_makes(makes: list[str]) -> list[str]:
    rest = list(makes)
    ordered: list[str] = []
    for canonical, variants in PRIORITY:
        for mk in list(rest):
            u = mk.upper().strip()
            if u in variants or (len(u) >= 3 and canonical.startswith(u)):
                ordered.append(mk)
                rest.remove(mk)
    return ordered + rest


def run_freshness_check(
    cfg: Config,
    client: TowClient,
    state: State,
    notifier: Notifier,
    now: datetime,
    max_requests: int = MAX_REQUESTS,
) -> Outcome:
    max_age = timedelta(hours=cfg.freshness_max_age_hours)
    newest: datetime | None = None
    http_errors: list[str] = []
    format_errors: list[str] = []
    scanned = 0
    live = False
    try:
        makes = client.list_makes()
        if not makes:
            raise SiteFormatError("make dropdown is empty")
        for make in order_makes(makes):
            if client.http.request_count >= max_requests:
                log.warning("request cap %d reached", max_requests)
                break
            try:
                res = client.search_make(make)
            except SiteFormatError as e:
                (http_errors if e.status is not None else format_errors).append(f"{make}: {e}")
                continue
            except DataSourceError as e:
                http_errors.append(f"{make}: {e}")
                continue
            scanned += 1
            for rec in res.records:
                store(state, rec, now, CHECK)
                if newest is None or rec.tow_datetime > newest:
                    newest = rec.tow_datetime
            if newest is not None and now - newest < max_age:
                live = True
                break
    except DataSourceError as e:
        format_errors.append(f"load lookup page: {e}")

    newest_txt = full_label(newest) if newest else "none seen"
    if format_errors or scanned == 0 or len(http_errors) > scanned:
        failing = (format_errors + http_errors)[:5]
        kind = "site_error"
        subject = "Tow lookup: site or format changed"
        body = (
            "The freshness check could not read the Towed Vehicle Lookup, so a clean plate check "
            "may not mean the car is safe.\n\n"
            "Failing step(s):\n  " + "\n  ".join(failing or ["no make could be searched"]) + "\n\n"
            f"Makes searched OK: {scanned}; HTTP errors: {len(http_errors)}; format errors: "
            f"{len(format_errors)}. Newest tow seen: {newest_txt}.\n\nLookup: {LOOKUP_URL}"
        )
        short = f"Tow lookup check failed: {failing[0][:110] if failing else 'no data'}"
    elif live:
        kind = "ok"
        detail = (
            f"site is live, newest tow at {newest_txt} "
            f"({scanned} make(s), {client.http.request_count} requests)"
        )
        prev = state.recent_runs(CHECK, limit=1)
        if prev and prev[0].outcome in ("stale", "site_error"):
            notifier.notify(
                "tow-data-recovered",
                "Tow lookup recovered",
                f"The Towed Vehicle Lookup is live again: {detail}.",
                f"freshness:recovered:{now.date().isoformat()}",
            )
        return Outcome("ok", detail)
    else:
        kind = "stale"
        subject = "Tow lookup data may be stale"
        body = (
            f"A scan of {scanned} make(s) found no tow newer than {cfg.freshness_max_age_hours} hours. "
            f"Newest tow seen: {newest_txt}.\n\nThis may be a quiet period, or the lookup may have "
            f"stopped receiving new tows. Until it recovers, a clean plate check means less.\n\n"
            f"Lookup: {LOOKUP_URL}"
        )
        short = f"Tow lookup may be stale: no tow in {cfg.freshness_max_age_hours}h, newest {newest_txt}"

    notifier.notify(
        "tow-data-stale" if kind == "stale" else "site-changed",
        subject,
        body,
        f"freshness:{kind}:{now.date().isoformat()}",
        short=short,
    )
    if kind == "stale":
        return Outcome("stale", f"no tow in {cfg.freshness_max_age_hours}h, newest tow at {newest_txt}")
    return Outcome("site_error", "; ".join((format_errors + http_errors)[:5]) or "nothing searchable")
