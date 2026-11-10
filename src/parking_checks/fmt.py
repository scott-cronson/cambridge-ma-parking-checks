"""Human-readable dates and times (portable: no %-d, which Windows strftime lacks)."""

from __future__ import annotations

from datetime import date, datetime

from . import ET


def day_label(d: date) -> str:
    """Fri Dec 4"""
    return f"{d:%a} {d:%b} {d.day}"


def weekday(d: date) -> str:
    return f"{d:%A}"


def time_label(dt: datetime) -> str:
    """6:12 pm (in Eastern time)"""
    dt = dt.astimezone(ET)
    h = dt.hour % 12 or 12
    return f"{h}:{dt:%M} {'am' if dt.hour < 12 else 'pm'}"


def when_label(dt: datetime) -> str:
    """Wed 6:12 pm"""
    dt = dt.astimezone(ET)
    return f"{dt:%a} {time_label(dt)}"


def full_label(dt: datetime) -> str:
    """Wed Dec 2, 6:12 pm"""
    dt = dt.astimezone(ET)
    return f"{day_label(dt.date())}, {time_label(dt)}"
