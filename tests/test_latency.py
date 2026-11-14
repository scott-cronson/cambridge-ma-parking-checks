from parking_checks.tow.client import TowRecord
from parking_checks.tow.latency import latency_report
from parking_checks.tow.plate_check import store
from tests.conftest import et


def seen(state, plate, towed, at):
    store(state, TowRecord(plate=plate, tow_datetime=et(towed), make="TOYT"), et(at), "tow-sweep")


def test_report_separates_late_entries(state):
    run = state.start_run("tow-sweep", et("2026-11-10T15:00"))
    seen(state, "OLD1", "2026-11-09T10:00", "2026-11-10T15:00")
    state.finish_run(run, et("2026-11-10T15:01"), "ok", "")
    seen(state, "NEW1", "2026-11-11T09:00", "2026-11-11T11:00")
    seen(state, "NEW2", "2026-11-11T09:00", "2026-11-11T12:00")
    seen(state, "LATE", "2026-11-01T09:00", "2026-11-11T09:00")
    out = latency_report(state)
    assert "Tows timed: 3" in out
    assert "Posted within 24 h: 2 of 3 (median 2.5 h, worst 3.0 h)" in out
    assert "Late entries: 1 of 3 listed 240-240 h" in out
    assert "second tow signal" not in out
