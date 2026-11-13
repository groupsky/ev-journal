"""Tyre sets with made-up sets and temperatures (#45)."""

from datetime import date

import pytest

from ev_journal.tyres import Rules, age_years, dot_date, km_per_set, overview

TODAY = date(2030, 6, 15)
RULES = Rules()


def tset(id_, season, dot=None, retired=0):
    return {
        "id": id_,
        "name": f"Test {id_}",
        "season": season,
        "size": None,
        "dot": dot,
        "retired": retired,
        "note": None,
    }


def swap(set_id, km, day="2030-01-01"):
    return {"set_id": set_id, "km": km, "date": day}


def test_km_per_set_over_three_swaps():
    swaps = [swap("w", 10_000), swap("s", 15_000), swap("w", 25_000)]
    assert km_per_set(swaps, 27_500) == {"w": 7_500, "s": 10_000}


def test_the_set_on_now_runs_to_km_now():
    assert km_per_set([swap("s", 1_000)], 1_800) == {"s": 800}
    assert km_per_set([swap("s", 1_000)], None) == {"s": 0}


def test_age_from_dot():
    assert dot_date("0721") == date(2021, 2, 15)  # Monday of week 7 of 2021
    assert age_years("0721", date(2027, 2, 15)) == 6.0
    assert age_years(None, TODAY) is None


@pytest.mark.parametrize("bad", ["721", "07211", "ab21", "0021", "5421", "5321"])
def test_bad_dot(bad):
    # 2021 has 52 ISO weeks, so week 53 of 2021 does not exist either.
    with pytest.raises(ValueError):
        dot_date(bad)


def test_age_warning_and_fitted():
    sets = [tset("old", "summer", "0721"), tset("new", "winter", "1029")]
    o = overview(
        sets,
        [swap("new", 1, "2029-11-01"), swap("old", 9, "2030-04-01")],
        date(2027, 6, 1),
        10,
        [],
        RULES,
    )
    assert o["fitted"]["id"] == "old" and o["fitted"]["fitted_since"] == "2030-04-01"
    assert o["age_warning"] == ["Test old"]
    by = {s["id"]: s for s in o["sets"]}
    assert by["new"]["fitted_since"] is None and by["new"]["km"] == 8
    assert not by["new"]["age_warning"]
    # A retired set is not warned about.
    sets[0]["retired"] = 1
    assert overview(sets, [], date(2028, 1, 1), None, [], RULES)["age_warning"] == []


def hint_of(fitted, other, temps):
    sets = [tset("on", fitted)] + ([tset("off", other[0], retired=other[1])] if other else [])
    return overview(sets, [swap("on", 0)], TODAY, 0, temps, RULES)["hint"]


def test_hint_winter_and_summer():
    cold, warm = [12, 12, 5, 6, 4, 6, 5, 6, 5], [3, 9, 8, 10, 9, 8, 7.5]
    assert hint_of("summer", ("winter", 0), cold) == "winter"
    assert hint_of("summer", ("winter", 0), warm) is None
    assert hint_of("winter", ("summer", 0), warm) == "summer"
    assert hint_of("winter", ("summer", 0), cold) is None


def test_no_hint():
    cold = [5] * 7
    assert hint_of("all", ("winter", 0), cold) is None  # an all-season set on
    assert hint_of("summer", None, cold) is None  # only one set
    assert hint_of("summer", ("winter", 1), cold) is None  # the other one is retired
    assert hint_of("summer", ("winter", 0), cold[:6]) is None  # fewer than 7 days
    assert overview([tset("a", "summer")], [], TODAY, 0, cold, RULES)["hint"] is None
