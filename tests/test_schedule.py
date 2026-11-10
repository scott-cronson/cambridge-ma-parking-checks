from datetime import date

from parking_checks.cleaning.schedule import month_rule, nth_weekday, rule_dates


def test_nth_weekday():
    assert nth_weekday(2026, 12, 5, 1) == date(2026, 12, 4)  # 1st Friday
    assert nth_weekday(2026, 12, 1, 2) == date(2026, 12, 14)  # 2nd Monday
    assert nth_weekday(2026, 11, 4, 2) == date(2026, 11, 12)  # 2nd Thursday


def test_district_c_december_2026(sched):
    odd = rule_dates(sched, "C", "odd", date(2026, 12, 1), date(2026, 12, 31))
    even = rule_dates(sched, "C", "even", date(2026, 12, 1), date(2026, 12, 31))
    assert [c.date for c in odd] == [date(2026, 12, 4)]
    assert [c.date for c in even] == [date(2026, 12, 14)]
    assert not odd[0].holiday_conflict


def test_district_b_odd_september_2026_holiday_conflict(sched):
    mr = month_rule(sched, "B", "odd", 2026, 9)
    assert mr.rule_date == date(2026, 9, 7)
    assert mr.holiday_conflict and mr.holiday == "Labor Day"
    assert mr.actual_date == date(2026, 8, 31)
    # The fallback returns the confirmed relocated date, not the holiday.
    got = rule_dates(sched, "B", "odd", date(2026, 8, 25), date(2026, 9, 30))
    assert [(c.date, c.source) for c in got] == [(date(2026, 8, 31), "rule_relocated")]
    assert got[0].rule_date == date(2026, 9, 7)


def test_unconfirmed_holiday_conflict_is_flagged(sched):
    # 2027-07-05 is Independence Day observed; the city has not published the move.
    got = rule_dates(sched, "B", "odd", date(2027, 7, 1), date(2027, 7, 31))
    assert [c.date for c in got] == [date(2027, 7, 5)]
    assert got[0].holiday_conflict and got[0].source == "rule"


def test_no_cleaning_january_to_march(sched):
    assert rule_dates(sched, "C", "odd", date(2027, 1, 1), date(2027, 3, 31)) == []
