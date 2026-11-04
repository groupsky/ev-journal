import random
from dataclasses import replace
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from ev_journal import forecast
from ev_journal.forecast import advise, backtest, day_type, message, night_zone, plan
from ev_journal.journeys import Journey, Result, Trip

UTC = ZoneInfo("UTC")
KPP = 0.30  # kWh per %
WINTER = night_zone(date(2026, 1, 12), UTC)  # 22:00–06:00
SUMMER = night_zone(date(2026, 7, 13), UTC)  # 23:00–07:00


def at(d, h, m=0):
    return datetime.combine(d, time(h, m), UTC)


def test_night_zones():
    assert WINTER == (at(date(2026, 1, 12), 22), at(date(2026, 1, 13), 6))
    assert SUMMER == (at(date(2026, 7, 13), 23), at(date(2026, 7, 14), 7))
    # The zone of the evening's date: 31 March is winter, 1 April summer.
    assert night_zone(date(2026, 3, 31), UTC)[0].hour == 22
    assert night_zone(date(2026, 4, 1), UTC)[0].hour == 23
    assert night_zone(date(2026, 10, 31), UTC)[0].hour == 23
    assert night_zone(date(2026, 11, 1), UTC)[0].hour == 22


def test_low_level_reaches_the_target():
    # 6 kWh bad day = 20 % + 10 % reserve = 30 %; from 20 %: 10 % = 3 kWh, 3.26 h at low.
    a = advise(20, 6.0, KPP, WINTER, None, 10)
    assert (a.target, a.level, a.capped) == (30, "low", False)
    assert a.end == WINTER[1]  # as late as the zone allows
    assert a.start == a.end - timedelta(hours=3, minutes=30)  # rounded up to 15 min


def test_only_mid_or_high_reaches():
    # 15 kWh = 50 % + 10 % = 60 %; from 20 %: 40 % = 12 kWh.
    # Summer zone 8 h: low 13.0 h, mid 7.5 h (fits).
    a = advise(20, 15.0, KPP, SUMMER, None, 10)
    assert (a.target, a.level) == (60, "mid")
    assert a.start == SUMMER[1] - timedelta(hours=7, minutes=30)
    # Departure inside the zone leaves 6 h: only high (5.13 h) fits.
    a = advise(20, 15.0, KPP, SUMMER, at(date(2026, 7, 14), 5), 10)
    assert (a.level, a.end) == ("high", at(date(2026, 7, 14), 5))
    assert a.start == a.end - timedelta(hours=5, minutes=15)


def test_target_above_what_the_zone_reaches():
    a = advise(10, 27.0, KPP, WINTER, None, 10)  # wants 100 %
    assert a.capped and a.level == "high"
    assert (a.start, a.end) == WINTER
    assert a.target == 10 + int(8 * 2.34 / KPP)  # 72 %


def test_no_charge_needed():
    a = advise(55, 6.0, KPP, WINTER, None, 10)
    assert a.level is None and a.target == 30
    f = forecast.Forecast(date(2026, 1, 13), "workday", 4.0, 6.0)
    assert message(f, 55, a, False) == (
        "Утре: около 4 kWh (лош ден 6). Сега 55 %. Не е нужно зареждане тази вечер."
    )


def test_message_with_a_charge():
    f = forecast.Forecast(date(2026, 1, 13), "workday", 4.0, 6.0)
    a = advise(20, 6.0, KPP, WINTER, None, 10)
    assert message(f, 20, a, False) == (
        "Утре: около 4 kWh (лош ден 6). Сега 20 %. Нужни 30 % при тръгване."
        " Задай: ниско, 02:30–06:00."
    )


def test_day_types():
    hol = {date(2026, 1, 14)}
    assert day_type(date(2026, 1, 13), hol) == "workday"
    assert day_type(date(2026, 1, 14), hol) == "holiday"
    assert day_type(date(2026, 1, 17), hol) == "weekend"


def test_forecast_and_backtest():
    # Workdays 5 kWh, weekend days 20 kWh, 6 weeks from Monday 2026-01-05.
    d0 = date(2026, 1, 5)
    need = {d0 + timedelta(days=i): 0.0 for i in range(42)}
    for d in need:
        need[d] = 20.0 if d.weekday() >= 5 else 5.0
    f = forecast.forecast(need, date(2026, 2, 16), set())
    assert (f.day_type, f.normal_kwh, f.bad_kwh) == ("workday", 5.0, 7.0)
    f = forecast.forecast(need, date(2026, 2, 14), set())
    assert (f.day_type, f.normal_kwh, f.bad_kwh) == ("weekend", 20.0, 22.0)
    fs = backtest(need, set())
    assert len(fs) == 42 - 6  # each pool needs 3 days of its kind first
    sc = forecast.score(fs)
    assert sc["all"]["covered_pct"] > 90


def result(days: int, kwh: float, not_me_day: int | None = None) -> Result:
    d0 = date(2026, 1, 5)  # a Monday
    trips, journeys = [], []
    for i in range(days):
        t0 = int(at(d0 + timedelta(days=i), 8).timestamp() * 1000)
        t = Trip(start=t0, end=t0 + 3_600_000, kwh=kwh)
        trips.append(t)
        journeys.append(Journey(trips=[t], stops=[], not_me=i == not_me_day))
    return Result(trips=trips, journeys=journeys, outings=[], months=[])


def test_not_me_days_are_left_out():
    r = result(10, 5.0, not_me_day=2)
    need = forecast.daily_need(r, UTC, date(2026, 1, 14))
    assert need[date(2026, 1, 7)] == 0.0
    assert need[date(2026, 1, 8)] == 5.0


def test_plan_on_a_holiday_with_preheat():
    r = result(28, 5.0)
    now = int(at(date(2026, 2, 2), 20).timestamp() * 1000)  # evening before Tuesday 3rd
    hols = forecast.holiday_set(None, ("2026-02-03",), range(2026, 2027))
    a = plan(r, UTC, now, 50.0, KPP, hols, departure="05:15", preheat=True)
    assert a["day"] == "2026-02-03" and a["day_type"] == "holiday"
    assert a["preheat_off"] is True
    assert a["message"].endswith("Утре е почивен ден: изключи затоплянето.")
    assert r.forecasts and r.forecasts[-1].real_kwh is not None
    # A workday: no preheat message, and the departure ends the charge.
    a = plan(r, UTC, now, 5.0, KPP, set(), departure="05:00", preheat=True)
    assert a["day_type"] == "workday" and not a["preheat_off"]
    assert a["end"] == at(date(2026, 2, 3), 5).isoformat()


def test_plan_after_midnight_is_for_today():
    r = result(28, 5.0)
    now = int(at(date(2026, 2, 3), 1).timestamp() * 1000)
    assert plan(r, UTC, now, 50.0, KPP, set())["day"] == "2026-02-03"


def test_plan_without_soc_or_history():
    now = int(at(date(2026, 2, 2), 20).timestamp() * 1000)
    assert plan(result(28, 5.0), UTC, now, None, KPP, set()) is None
    assert plan(result(0, 5.0), UTC, now, 50.0, KPP, set()) is None


def test_official_holidays():
    pytest.importorskip("holidays")
    hols = forecast.holiday_set("GB", (), range(2026, 2027))
    assert date(2026, 12, 25) in hols


def test_api(client, advice):
    assert client.get("/advice").status_code == 404
    r = result(28, 5.0)
    now = int(at(date(2026, 2, 2), 20).timestamp() * 1000)
    advice["value"] = plan(r, UTC, now, 20.0, KPP, set())
    got = client.get("/advice")  # checked against docs/openapi.json
    assert got.status_code == 200
    assert got.json()["level"] == "low"
    assert got.json()["score"]["all"]["days"] == len(r.forecasts)


DST = ZoneInfo("Europe/Lisbon")  # any zone with EU summer time


def real_h(a):
    """Real hours between start and end (same-zone subtraction uses clock time)."""
    return (a.end.astimezone(UTC) - a.start.astimezone(UTC)).total_seconds() / 3600


def test_the_nights_the_clocks_change():
    # 2026-03-28/29: the zone is 7 real hours.
    z = night_zone(date(2026, 3, 28), DST)
    a = advise(20, 9.0, KPP, z, None, 10)  # wants 40 %
    assert a.level == "low"  # 6.52 h → 6.75 h fits in 7 h
    assert real_h(a) == 6.75
    assert a.start.hour == 22 and a.start.minute == 15
    a = advise(10, 27.0, KPP, z, None, 10)
    assert a.capped and a.target == 10 + int(7 * 2.34 / KPP)  # 64 %, not 72 %
    # 2026-10-24/25: 9 real hours.
    z = night_zone(date(2026, 10, 24), DST)
    a = advise(20, 9.0, KPP, z, None, 15)  # wants 45 %: 25 % = 7.5 kWh, 8.15 h at low
    assert a.level == "low" and a.start.utcoffset() != a.end.utcoffset()
    assert real_h(a) == 8.25


def test_departure_after_the_zone_ends():
    a = advise(20, 6.0, KPP, WINTER, at(date(2026, 1, 13), 8), 10)
    assert a.end == WINTER[1]


def test_after_midnight_the_start_is_not_in_the_past():
    now = at(date(2026, 1, 13), 3)
    a = advise(20, 6.0, KPP, WINTER, None, 10, now)  # 3.5 h at low does not fit in 3 h
    assert a.level == "mid" and a.start >= now


def test_timer_step():
    # 3.26 h at low: 3 h 20 min in 10 min steps, 3 h 30 min in 15 min steps.
    a = advise(20, 6.0, KPP, WINTER, None, 10, step_min=10)
    assert a.start == a.end - timedelta(hours=3, minutes=20)


def test_departure_per_day_type():
    r = result(28, 5.0)
    evening = at(date(2026, 2, 2), 20)  # before Tuesday 2026-02-03
    now = int(evening.timestamp() * 1000)
    ranges = (("alt", date(2026, 2, 1), date(2026, 2, 10)),)
    deps = {"alt": "05:30", "weekend": None}
    a = plan(r, UTC, now, 5.0, KPP, set(), "05:00", departures=deps, ranges=ranges)
    assert a["day_type"] == "alt"
    assert a["end"] == a["departure"] == at(date(2026, 2, 3), 5, 30).isoformat()
    # Outside the range: the default departure.
    a = plan(r, UTC, now, 5.0, KPP, set(), "05:00", departures=deps)
    assert a["day_type"] == "workday" and a["end"] == at(date(2026, 2, 3), 5).isoformat()
    # A weekend inside the range stays a weekend; "none" ends at the zone end.
    sat = int(at(date(2026, 2, 6), 20).timestamp() * 1000)
    a = plan(r, UTC, sat, 5.0, KPP, set(), "05:00", departures=deps, ranges=ranges)
    assert a["day_type"] == "weekend" and a["departure"] is None
    assert a["end"] == at(date(2026, 2, 7), 6).isoformat()


def weekday_need(weeks: int) -> dict[date, float]:
    """Mondays 15 kWh, other workdays 5, weekends 0, from Monday 2026-01-05."""
    d0 = date(2026, 1, 5)
    need = {}
    for i in range(weeks * 7):
        d = d0 + timedelta(days=i)
        need[d] = 0.0 if d.weekday() >= 5 else 15.0 if d.weekday() == 0 else 5.0
    return need


def test_pool_by_weekday_and_fallback():
    need = weekday_need(10)
    mon = date(2026, 3, 16)
    f = forecast.forecast(need, mon, set(), choices={"workday": ("weekday",)})
    assert (f.choice, f.normal_kwh) == (("weekday",), 15.0)
    # Only 2 past Mondays: too few, so the pool falls back to all workdays.
    f = forecast.forecast(need, date(2026, 1, 19), set(), choices={"workday": ("weekday",)})
    assert (f.choice, f.normal_kwh) == ((), 5.0)
    # A pair falls back to its first key.
    f = forecast.forecast(need, mon, set(), choices={"workday": ("weekday", "month")})
    assert (f.choice, f.normal_kwh) == (("weekday",), 15.0)


def test_named_range_pools_from_the_same_name():
    need = weekday_need(10)
    ranges = (
        ("alt", date(2026, 2, 3), date(2026, 2, 6)),
        ("alt", date(2026, 3, 1), date(2026, 3, 31)),
    )
    for d in need:  # the "alt" workdays need 30 kWh; the others 5 or 15
        if any(a <= d <= b for _n, a, b in ranges) and d.weekday() < 5:
            need[d] = 30.0
    # The plain 28-day pool would give 5: most of its days are not "alt".
    assert forecast.forecast(need, date(2026, 3, 3), set()).normal_kwh == 5.0
    f = forecast.forecast(need, date(2026, 3, 3), set(), ranges)
    assert (f.day_type, f.choice, f.normal_kwh) == ("alt", ("day_type",), 30.0)
    # The first days of a range: too few past "alt" days, so all workdays.
    ranges = (("alt", date(2026, 2, 3), date(2026, 2, 4)),)
    f = forecast.forecast(need, date(2026, 2, 4), set(), ranges)
    assert (f.day_type, f.choice, f.normal_kwh) == ("alt", (), 5.0)
    # A weekend inside a range stays a weekend.
    f = forecast.forecast(
        need, date(2026, 3, 7), set(), (("alt", date(2026, 3, 1), date(2026, 3, 31)),)
    )
    assert f.day_type == "weekend"


def test_pick_keeps_a_key_only_when_it_beats_no_key():
    picks, rows = forecast.pick(weekday_need(30), set())
    assert picks["workday"] == ("weekday",)
    assert picks["off"] == ()  # all off days are 0: no key can do better
    tried = {(r["kind"], r["choice"]) for r in rows}
    assert ("workday", ()) in tried and ("workday", ("month",)) in tried
    assert sum(r["picked"] for r in rows) == 2
    # Every workday the same: nothing beats no key.
    flat = {d: 0.0 if d.weekday() >= 5 else 5.0 for d in weekday_need(30)}
    picks, rows = forecast.pick(flat, set())
    assert picks["workday"] == ()


def test_pick_tries_the_pair_of_the_two_best_keys(monkeypatch):
    # When no single key beats no key, the pair of the two best is tried.
    monkeypatch.setattr(forecast, "beats", lambda s, base: len(s["choice"]) == 2)
    picks, rows = forecast.pick(weekday_need(30), set())
    singles = sorted(
        (r for r in rows if r["kind"] == "workday" and len(r["choice"]) == 1),
        key=lambda r: r["normal_error_kwh"],
    )
    assert picks["workday"] == singles[0]["choice"] + singles[1]["choice"]


def test_dry_run_prints_every_tried_choice(monkeypatch, capsys, tmp_path):
    from types import SimpleNamespace

    from ev_journal import __main__ as cli

    r = Result(trips=[], journeys=[], outings=[], months=[])
    _picks, r.forecast_tries = forecast.pick(weekday_need(30), set())
    monkeypatch.setattr(cli, "compute", lambda s: r)
    monkeypatch.setattr(cli, "away", lambda *a: "-")
    cli.dry_run(SimpleNamespace(data_dir=tmp_path))
    out = capsys.readouterr().out
    assert "workday no key" in out and "weekday" in out and "month_part" in out
    assert out.count("<- picked") == 2


def noisy_need(plant, seed: int = 1, days: int = 730) -> dict[date, float]:
    """Workdays 8 kWh, off days 2, plus noise, plus plant(day) kWh."""
    rnd = random.Random(seed)
    d0 = date(2025, 1, 6)
    need = {}
    for i in range(days):
        d = d0 + timedelta(days=i)
        x = (2.0 if d.weekday() >= 5 else 8.0) + rnd.gauss(0, 1.5) + plant(d)
        need[d] = max(x, 0.0)
    return need


def base_of(need):
    return backtest(need, set(), days=len(need))


@pytest.mark.parametrize(
    "plant, family",
    [
        (lambda d: 3.0 if d.month % 2 else 0.0, {"month_mod2", "month"}),
        (lambda d: 4.0 if d.day == 5 else 0.0, {"day_of_month"}),
        (lambda d: 4.0 if (d.day - 1) // 7 == 0 and d.weekday() == 1 else 0.0, {"nth_weekday"}),
    ],
)
def test_a_planted_rhythm_is_found(plant, family):
    keys, rows = forecast.select(base_of(noisy_need(plant)))
    assert keys and keys[0] in family
    assert rows[-1]["normal_error_kwh"] < rows[0]["normal_error_kwh"]


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_pure_noise_picks_no_key(seed):
    keys, rows = forecast.select(base_of(noisy_need(lambda d: 0.0, seed)))
    assert keys == () and len(rows) == 1


def test_effects_shrink_and_need_a_count():
    m = forecast.Effects(("weekday",))
    assert m.effect([3.0] * (forecast.MIN_COUNT - 1)) == 0.0
    n = forecast.MIN_COUNT
    assert m.effect([3.0] * n) == pytest.approx(3.0 * n / (n + forecast.SHRINK))


def test_a_day_uses_only_the_days_before_it():
    base = base_of(noisy_need(lambda d: 4.0 if d.day == 5 else 0.0))
    fs, _m = forecast.walk(base, ("day_of_month",))
    day = fs[400].day
    # From that day on, every 5th needs 6 kWh more: the forecasts up to
    # that day stay the same, the later ones learn it.
    later = [
        replace(f, real_kwh=f.real_kwh + 6.0) if f.day >= day and f.day.day == 5 else f
        for f in base
    ]
    fs2, _m = forecast.walk(later, ("day_of_month",))
    assert [f for f in fs if f.day <= day] == [f for f in fs2 if f.day <= day]
    assert fs[-1].day.day == 5 and fs[-1].effect_kwh < fs2[-1].effect_kwh


def result_of(need: dict[date, float]) -> Result:
    trips = [
        Trip(start=int(at(d, 8).timestamp() * 1000), end=int(at(d, 9).timestamp() * 1000), kwh=x)
        for d, x in need.items()
    ]
    return Result(trips=trips, journeys=[], outings=[], months=[])


def test_plan_uses_the_effects_only_when_they_win(monkeypatch, capsys, tmp_path):
    from types import SimpleNamespace

    from ev_journal import __main__ as cli

    fifth = noisy_need(lambda d: 4.0 if d.day == 5 else 0.0, days=760)  # to 2027-02-04
    now = int(at(date(2027, 2, 4), 20).timestamp() * 1000)  # evening before the 5th
    r = result_of(fifth)
    a = plan(r, UTC, now, 50.0, KPP, set())
    assert r.forecast_effects["keys"] == ("day_of_month",)
    [e] = a["effects"]
    assert (e["key"], e["value"]) == ("day_of_month", 5) and e["effect_kwh"] > 1.0
    assert any(f.effect_kwh for f in r.forecasts)
    r.trips = []  # dry-run's month table needs months; not part of this test
    monkeypatch.setattr(cli, "compute", lambda s: r)
    monkeypatch.setattr(cli, "away", lambda *a: "-")
    cli.dry_run(SimpleNamespace(data_dir=tmp_path))
    out = capsys.readouterr().out
    assert "today's rule" in out and "day_of_month  " in out and "<- used" in out
    assert "day_of_month = 5 (workday): +" in out
    # Pure noise: today's rule stays, the same forecast as without the model.
    r = result_of(noisy_need(lambda d: 0.0, days=760))
    a = plan(r, UTC, now, 50.0, KPP, set())
    assert r.forecast_effects["keys"] == () and a["effects"] == []
    assert not any(f.effect_kwh for f in r.forecasts)
