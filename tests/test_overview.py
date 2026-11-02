import pytest
from test_price import DAY, T0, UTC, worked_example

from ev_journal import outputs
from ev_journal.journeys import Trip, build
from ev_journal.overview import overview


def test_per_kwh_home_and_away():
    # Home: 3 x 50 kWh for 10 EUR. Away: 10 kWh for 5 EUR.
    r, _ = worked_example(away=5.0)
    a = overview(r, T0 + 10 * DAY)["all"]
    assert a["eur_per_kwh"] == pytest.approx(35 / 160)
    assert a["eur_per_kwh_home"] == pytest.approx(0.20)
    assert a["eur_per_kwh_away"] == pytest.approx(0.50)
    assert a["charges_no_cost"] == 0


def test_per_100km():
    # Only the last trip (250 km, 10 EUR) has a price. All trips: 50 kWh per 250 km.
    r, _ = worked_example()
    a = overview(r, T0 + 10 * DAY)["all"]
    assert a["eur_per_100km"] == pytest.approx(4.0)
    assert a["kwh_per_100km"] == pytest.approx(20.0)
    assert a["trips_no_price"] == 2


def test_no_cost_is_left_out():
    # The away charge has no cost: it is counted, not guessed.
    r, _ = worked_example(away="unknown")
    a = overview(r, T0 + 10 * DAY)["all"]
    assert a["eur_per_kwh"] == pytest.approx(0.20)
    assert a["eur_per_kwh_away"] is None
    assert a["charges_no_cost"] == 1


def test_last_30_days():
    r, _ = worked_example()
    # 30 days after the second day: only the third charge and trip are inside.
    d = overview(r, T0 + 2 * DAY + 30 * DAY - 1)["last_30d"]
    assert d["eur_per_kwh"] == pytest.approx(0.20)
    assert d["eur_per_100km"] == pytest.approx(4.0)
    assert d["trips_no_price"] == 0
    # Nothing in the window: no numbers at all.
    e = overview(r, T0 + 100 * DAY)["last_30d"]
    assert e["eur_per_kwh"] is None and e["eur_per_100km"] is None and e["kwh_per_100km"] is None


def test_month_and_summary():
    r, _ = worked_example(away=5.0)
    jan, dec = {m.key: m for m in r.months}["2026-01"], {m.key: m for m in r.months}["2025-12"]
    assert jan.eur_per_kwh() == pytest.approx(0.20)
    assert jan.eur_per_kwh("away") is None
    assert dec.eur_per_kwh() == pytest.approx(0.50)
    assert dec.eur_per_kwh("home") is None
    s = outputs.summary(r, T0 + 3 * DAY, UTC)
    assert s["month"]["eur_per_kwh_home"] == 0.2
    assert s["overview"]["all"]["eur_per_kwh"] == round(35 / 160, 3)
    assert s["overview"]["last_30d"]["charges_no_cost"] == 0
    lines = [ln for ln in outputs.influx_lines(r) if ln.startswith("ev_month ")]
    assert any("eur_per_kwh_home=0.2" in ln for ln in lines)


def test_overview_matches_month():
    # A trip without kWh still counts its km, as in the month.
    trips = [
        Trip(start=T0, end=T0 + 3_600_000, kwh=None, km_homy=100.0),
        Trip(start=T0 + DAY, end=T0 + DAY + 3_600_000, kwh=20.0, km_homy=100.0),
    ]
    r = build(trips, [], [], [], UTC)
    assert r.months[0].kwh_per_100km == pytest.approx(10.0)
    assert overview(r, T0 + 10 * DAY)["all"]["kwh_per_100km"] == pytest.approx(10.0)
