"""1A. Plate check: search the lookup for the configured plate and alert on a hit."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from ..config import Config
from ..fmt import full_label
from ..health import Outcome
from ..notify import Notifier
from ..state import State
from .client import LOOKUP_URL, TowClient, TowRecord, normalize_plate

log = logging.getLogger(__name__)

POLICE_PHONE = "617-349-3300"
# From the lookup page's FAQ (Nov 2026). Only the city contract company is listed here.
COMPANY_PHONES = {"B&B": "617-492-5781 (B&B Tow, 50 Mooney St, Cambridge)"}
REMINDER_SLACK = timedelta(minutes=30)  # allow timer jitter around TOW_REMINDER_HOURS


def store(state: State, rec: TowRecord, now: datetime, by: str) -> bool:
    return state.record_tow(
        plate=rec.plate_norm,
        tow_datetime=rec.tow_datetime,
        first_seen_at=now,
        first_seen_by=by,
        make=rec.make,
        model=rec.model,
        towed_from=rec.location,
        tow_company=rec.tow_company,
        reason=rec.reason,
        tracking_no=rec.tracking_no,
    )


def _company_phone(company: str | None) -> str | None:
    if not company:
        return None
    for prefix, phone in COMPANY_PHONES.items():
        if company.upper().startswith(prefix):
            return phone
    return None


def tow_message(cfg: Config, rec: TowRecord, n: int) -> tuple[str, str, str]:
    plate = normalize_plate(cfg.plate)
    when = full_label(rec.tow_datetime)
    tag = "" if n == 0 else f" (reminder {n} of {cfg.tow_reminder_max})"
    subject = f"CAR TOWED: {plate} towed {when}{tag}"
    lines = [
        f"The Cambridge Towed Vehicle Lookup lists plate {plate}.",
        "",
        f"Towed:      {when} (Eastern)",
        f"Towed by:   {rec.tow_company or 'not listed'}",
        f"Reason:     {rec.reason or 'not listed'}",
        f"Location:   {rec.location or 'not listed'}",
        f"Vehicle:    {' '.join(x for x in (rec.make, rec.model) if x) or 'not listed'}",
        f"Tracking #: {rec.tracking_no or 'not listed'}",
        "",
        f"Lookup:     {LOOKUP_URL}",
        f"Questions:  Cambridge Police {POLICE_PHONE}",
    ]
    phone = _company_phone(rec.tow_company)
    if phone:
        lines.append(f"Tow company: {phone}")
    lines += ["", "Storage fees grow daily. Call the tow company to confirm and arrange pickup."]
    short = f"CAR TOWED {plate} {when} by {rec.tow_company or '?'}. Call {phone or POLICE_PHONE}{tag}"
    return subject, "\n".join(lines), short


def run_plate_check(
    cfg: Config, client: TowClient, state: State, notifier: Notifier, now: datetime
) -> Outcome:
    target = normalize_plate(cfg.plate)
    res = client.search_plate(target)

    matches: list[TowRecord] = []
    for rec in res.records:
        store(state, rec, now, "tow-plate")
        if rec.plate_norm == target:
            matches.append(rec)
        else:
            log.info("partial plate match ignored: %s (%s)", rec.plate_norm, rec.tow_datetime)

    if not matches:
        return Outcome("ok", f"no tow for {target} ({res.kind}, {len(res.records)} partial)")

    alerted = 0
    for rec in matches:
        if not rec.is_detail and rec.detail_url:
            detail = client.get_detail(rec.detail_url)
            if detail is not None and detail.plate_norm == target:
                detail.tow_datetime = rec.tow_datetime
                rec = detail
                store(state, rec, now, "tow-plate")
        prefix = f"tow:{target}:{rec.tow_datetime.isoformat()}:"
        sent = state.sent_with_prefix(prefix)
        n = len(sent)
        if n == 0:
            due = True
        elif n > cfg.tow_reminder_max:
            due = False
        else:
            due = now - sent[-1][1] >= timedelta(hours=cfg.tow_reminder_hours) - REMINDER_SLACK
        if due:
            subject, body, short = tow_message(cfg, rec, n)
            notifier.notify("car-towed", subject, body, f"{prefix}{n}", short=short)
            alerted += 1
    status = "alerted" if alerted else "ok"
    return Outcome(status, f"{target} listed in tow lookup ({len(matches)} record(s), {alerted} alert(s))")
