from parking_checks.cleaning.matcher import (
    OUTSIDE_COVERAGE,
    SCHEDULE_UNKNOWN,
    STREET_NOT_IN_FILE,
    normalize_street,
    resolve,
    split_address,
)
from tests.conftest import CENTER as HOME


def test_split_and_normalize():
    assert split_address("201 Franklin St") == (201, "Franklin St")
    assert split_address("201-203 Franklin St") == (201, "Franklin St")
    assert split_address("Franklin St") == (None, "Franklin St")
    assert normalize_street("franklin st.") == "Franklin Street"
    assert normalize_street("Sidney St Ext") == "Sidney Street Extension"
    assert normalize_street("Bishop Allen Dr") == "Bishop Allen Drive"


def test_201_franklin(sched):
    r = resolve(sched, "201 Franklin St", *HOME)
    assert r.pairs == {("C", "odd"), ("C", "even")}
    assert not r.flags


def test_300_franklin_gap_on_odd_side(sched):
    r = resolve(sched, "300 Franklin St", *HOME)
    assert r.pairs == {("E", "even"), ("C", "odd"), ("E", "odd")}


def test_100_brookline_even_side_unknown(sched):
    r = resolve(sched, "100 Brookline St", *HOME)
    assert r.pairs == {("C", "odd")}
    assert SCHEDULE_UNKNOWN in r.flags
    assert r.unknown_sides == {"even"}


def test_lansdowne_alias(sched):
    r = resolve(sched, "40 Lansdowne St", *HOME)
    assert r.street == "Landsdowne Street"
    assert r.pairs == {("C", "odd"), ("C", "even")}


def test_no_house_number_takes_every_segment(sched):
    r = resolve(sched, "Franklin St", *HOME)
    assert r.pairs == {("C", "odd"), ("E", "odd"), ("C", "even"), ("E", "even")}


def test_street_not_in_file_near_home(sched):
    r = resolve(sched, "12 Nowhere St", 42.3630, -71.1010)
    assert r.flags == {STREET_NOT_IN_FILE} and not r.pairs


def test_outside_coverage(sched):
    assert resolve(sched, "12 Nowhere St", 42.40, -71.12).flags == {OUTSIDE_COVERAGE}
    assert resolve(sched, "100 Franklin St", 42.35, -71.06, city="Boston").flags == {OUTSIDE_COVERAGE}
