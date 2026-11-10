"""Run bookkeeping and failure visibility.

Every run writes a row to `runs`. A check whose run ends in "error" (crash or unreachable data
source) two runs in a row sends one "cannot run" alert, then stays quiet until a run succeeds,
which sends one "recovered" note.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from .net import DataSourceError
from .notify import Notifier
from .state import State

log = logging.getLogger(__name__)

ERROR = "error"


@dataclass
class Outcome:
    status: str  # ok | alerted | stale | site_error | skipped | error
    detail: str


def _error_streak_start(runs) -> int | None:
    """runs newest first. Id of the oldest run in the leading run of errors, if any."""
    start = None
    for r in runs:
        if r.outcome != ERROR:
            break
        start = r.id
    return start


def run_check(
    name: str,
    state: State,
    notifier: Notifier,
    clock: Callable[[], datetime],
    fn: Callable[[], Outcome],
) -> Outcome:
    run_id = state.start_run(name, clock())
    try:
        out = fn()
    except DataSourceError as e:
        out = Outcome(ERROR, f"data source unavailable: {e}")
    except Exception as e:  # noqa: BLE001 - every crash must be recorded and surfaced
        log.exception("%s crashed", name)
        out = Outcome(ERROR, f"crashed: {type(e).__name__}: {e}")
    state.finish_run(run_id, clock(), out.status, out.detail)
    log.info("%s finished: %s: %s", name, out.status, out.detail)

    runs = state.recent_runs(name, limit=100)
    if out.status == ERROR:
        n = 0
        for r in runs:
            if r.outcome != ERROR:
                break
            n += 1
        if n >= 2:
            sid = _error_streak_start(runs)
            notifier.notify(
                "check-cannot-run",
                f"{name} cannot run",
                f"The {name} check has failed {n} runs in a row.\n\nLatest error: {out.detail}\n\n"
                "You will get one note when it recovers. Logs: journalctl -u 'parking-checks*'",
                f"cannot-run:{name}:{sid}",
                short=f"Parking check {name} cannot run: {out.detail[:100]}",
            )
    elif len(runs) >= 2 and runs[1].outcome == ERROR:
        sid = _error_streak_start(runs[1:])
        if sid is not None and notifier.already_sent(f"cannot-run:{name}:{sid}"):
            notifier.notify(
                "check-recovered",
                f"{name} recovered",
                f"The {name} check is running again. Latest result: {out.status}: {out.detail}",
                f"recovered:{name}:{sid}",
                short=f"Parking check {name} recovered.",
            )
    return out
