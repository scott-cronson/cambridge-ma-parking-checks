from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from parking_checks import ET
from parking_checks.cleaning.schedule import Schedule
from parking_checks.config import DEFAULT_SCHEDULE, Config
from parking_checks.notify import Notifier
from parking_checks.state import State

FIXTURES = Path(__file__).parent / "fixtures"
# A public reference point for tests (Central Square T station).
CENTER = (42.3654, -71.1037)


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class FakeChannel:
    def __init__(self, name: str, fail: bool = False):
        self.name = name
        self.fail = fail
        self.sent: list[tuple[str, str, str]] = []

    def send(self, subject: str, body: str, short: str) -> None:
        if self.fail:
            raise RuntimeError(f"{self.name} down")
        self.sent.append((subject, body, short))


class Clock:
    def __init__(self, now: datetime):
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def set(self, iso: str) -> None:
        self.now = et(iso)


def et(iso: str) -> datetime:
    return datetime.fromisoformat(iso).replace(tzinfo=ET)


@pytest.fixture
def cfg() -> Config:
    c = Config(
        plate="TEST123",
        intake_token="t" * 32,
        schedule_path=DEFAULT_SCHEDULE,
        db_path=Path(":memory:"),
        coverage_center=CENTER,
    )
    c.validate()
    return c


@pytest.fixture
def sched() -> Schedule:
    return Schedule.load(DEFAULT_SCHEDULE, CENTER)


@pytest.fixture
def state():
    s = State(":memory:")
    yield s
    s.close()


@pytest.fixture
def clock() -> Clock:
    return Clock(et("2026-11-10T12:00"))


@pytest.fixture
def channel() -> FakeChannel:
    return FakeChannel("email")


@pytest.fixture
def notifier(state, channel, clock) -> Notifier:
    return Notifier(state, [channel], clock=clock)
