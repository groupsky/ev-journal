from datetime import date, timedelta

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


def test_families_are_generated():
    assert set(ck.FAMILIES) == {
        "weekday",
        "day_of_month",
        "day_from_month_end",
        "nth_weekday",
        "week_mod2",
        "week_mod3",
        "week_mod4",
        "month_mod2",
        "month_mod3",
        "month_mod4",
        "month",
        "day_type",
    }


def test_day_of_month():
    assert ck.day_of_month(date(2026, 3, 5), "workday") == 5


def test_day_from_month_end():
    assert ck.day_from_month_end(date(2026, 2, 28), "weekend") == 1
    assert ck.day_from_month_end(date(2026, 12, 31), "workday") == 1
    assert ck.day_from_month_end(date(2026, 12, 30), "workday") == 2
    assert ck.day_from_month_end(date(2028, 2, 1), "workday") == 29  # leap year


def test_nth_weekday():
    assert ck.nth_weekday(MON, "workday") == (1, 0)  # first Monday of February
    assert ck.nth_weekday(date(2026, 2, 10), "workday") == (2, 1)  # second Tuesday
    assert ck.nth_weekday(date(2026, 3, 31), "workday") == (5, 1)


def test_week_mod():
    f = ck.FAMILIES["week_mod3"]
    week = [f(MON + timedelta(days=7 * i), "workday") for i in range(6)]
    assert week[:3] == week[3:] and len(set(week)) == 3
    # the same value all week, Monday to Sunday
    assert {f(MON + timedelta(days=i), "workday") for i in range(7)} == {week[0]}
    # no break at the new year: 2026-12-28 and 2027-01-04 are a week apart
    g = ck.FAMILIES["week_mod2"]
    assert g(date(2026, 12, 28), "workday") != g(date(2027, 1, 4), "workday")


def test_month_mod():
    f = ck.FAMILIES["month_mod3"]
    vals = [f(date(2026 + (m - 1) // 12, (m - 1) % 12 + 1, 1), "workday") for m in range(10, 16)]
    assert vals[:3] == vals[3:] and len(set(vals)) == 3  # Oct 2026 … Mar 2027
    assert ck.FAMILIES["month_mod2"](date(2026, 12, 1), "workday") != ck.FAMILIES["month_mod2"](
        date(2027, 1, 1), "workday"
    )
