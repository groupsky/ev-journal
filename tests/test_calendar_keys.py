from datetime import date

from ev_journal import calendar_keys as ck

MON = date(2026, 2, 2)  # a Monday, ISO week 6


def test_weekday():
    assert ck.weekday(MON, "workday") == 0
    assert ck.weekday(date(2026, 2, 8), "weekend") == 6


def test_iso_week_parity():
    assert ck.iso_week_parity(MON, "workday") == 0
    assert ck.iso_week_parity(date(2026, 2, 9), "workday") == 1
    # 2026-01-01 is in ISO week 1: parity follows the ISO week, not the year
    assert ck.iso_week_parity(date(2026, 1, 1), "workday") == 1


def test_month_parity():
    assert ck.month_parity(MON, "workday") == 0
    assert ck.month_parity(date(2026, 3, 2), "workday") == 1


def test_month_part():
    assert [ck.month_part(date(2026, 1, d), "workday") for d in (1, 10, 11, 20, 21, 31)] == [
        "first",
        "first",
        "middle",
        "middle",
        "last",
        "last",
    ]


def test_month():
    assert ck.month(MON, "workday") == 2


def test_day_type():
    assert ck.day_type(MON, "alt") == "alt"
    assert ck.day_type(MON, "workday") == "workday"


def test_value_and_window():
    assert ck.value((), MON, "workday") == ()
    assert ck.value(("weekday", "month_part"), MON, "workday") == (0, "first")
    assert ck.window((), 28) == 28
    assert ck.window(("weekday", "month"), 28) == 365
