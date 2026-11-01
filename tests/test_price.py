from zoneinfo import ZoneInfo

import pytest

from ev_journal import charges, places, price
from ev_journal.journeys import Charge, Trip, build

UTC = ZoneInfo("UTC")
DAY = 86_400_000
T0 = 1_767_225_600_000  # 2026-01-01 00:00 UTC


def worked_example(away=None):
    """The car arrives with all-unknown energy at 30 %. Three times: it charges
    30 -> 80 % with priced energy (50 kWh for 10 EUR), then drives back to 30 %.
    Each drive is 50 kWh and 250 km. kWh per % is 1 (a home charge teaches it).

    away: None (no away charge), "unknown", or the money paid for a first
    away charge that brings it from 20 to 30 % before all that.
    """
    charges, trips, costs = [], [], {}
    if away is not None:
        c0 = Charge(start=T0 - DAY, end=T0 - DAY + 3_600_000, kwh=10.0, soc_start=20, soc_end=30)
        charges.append(c0)
        if away != "unknown":
            costs[c0.start] = away
    for i in range(3):
        start = T0 + i * DAY
        charges.append(
            Charge(start=start, end=start + 3_600_000, kwh=50.0, soc_start=30, soc_end=80)
        )
        costs[start] = 10.0
        trips.append(
            Trip(start=start + 2 * 3_600_000, end=start + 6 * 3_600_000, kwh=50.0, km_homy=250.0)
        )
    r = build(trips, charges, [], [], UTC)
    home = {c.start for c in charges[-3:]}
    k = price.apply(r, costs, home, UTC)
    return r, k


def test_worked_example():
    r, k = worked_example()
    assert k == 1.0
    shares = [t.unknown_share for t in r.trips]
    assert shares == pytest.approx([30 / 80, 30 * (30 / 80) / 80, 30 * (30 * 30 / 80 / 80) / 80])
    assert [round(s * 100) for s in shares] == [38, 14, 5]  # 37.5 % -> 14 % -> 5 %
    # Above 10 %: no price. At 5 %: priced from the known part, marked estimated.
    assert r.trips[0].cost_eur is None and r.trips[1].cost_eur is None
    t = r.trips[2]
    assert t.price_eur_per_kwh == pytest.approx(0.20)
    assert t.cost_eur == pytest.approx(10.0)
    assert t.price_estimated


def test_journey_and_month():
    r, _ = worked_example()
    m = r.months[0]
    assert m.spent_eur == pytest.approx(30.0)
    assert m.driving_cost_eur == pytest.approx(10.0)
    assert m.eur_per_100km == pytest.approx(10.0 / 250 * 100)  # priced km only
    assert m.kwh_per_100km == pytest.approx(150 / 750 * 100)
    assert m.drain_kwh == pytest.approx(0.0)
    assert [j.cost_eur for j in r.journeys][-1] == pytest.approx(10.0)
    assert r.journeys[0].cost_eur is None


def test_late_receipt_reprices_old_trips():
    # Without the away charge's cost, 10 of the first 80 kWh is unknown from it,
    # plus the 20 % it arrived with.
    before, _ = worked_example(away="unknown")
    late, _ = worked_example(away=3.0)  # the receipt came later; a rerun
    # Trip 2 had no price; with the receipt it has one.
    assert before.trips[1].cost_eur is None
    assert late.trips[1].unknown_share < 0.10 < before.trips[1].unknown_share
    assert late.trips[1].cost_eur is not None
    # Trip 3 had a price; the receipt changes it.
    assert before.trips[2].cost_eur is not None
    assert late.trips[2].price_eur_per_kwh != pytest.approx(before.trips[2].price_eur_per_kwh)


def test_the_10_percent_cut():
    def one(unknown):
        c1 = Charge(start=T0, end=T0 + 1, kwh=100.0 - unknown)  # no SoC: no reset
        c0 = Charge(start=T0 - 10, end=T0 - 5, kwh=unknown, soc_start=None, soc_end=None)
        t = Trip(start=T0 + 10, end=T0 + 20, kwh=1.0)
        r = build([t], [c0, c1], [], [], UTC)
        price.apply(r, {c1.start: (100.0 - unknown) * 0.25}, {c1.start}, UTC)
        return r.trips[0]

    assert one(10.0).cost_eur == pytest.approx(0.25) and one(10.0).price_estimated
    assert one(10.1).cost_eur is None
    exact = one(0.0)
    assert exact.cost_eur == pytest.approx(0.25) and not exact.price_estimated


def test_drain_while_parked():
    c1 = Charge(start=T0, end=T0 + 1, kwh=50.0, soc_start=30, soc_end=80)
    c2 = Charge(start=T0 + DAY, end=T0 + DAY + 1, kwh=10.0, soc_start=78, soc_end=88)
    r = build([], [c1, c2], [], [], UTC)
    price.apply(r, {c1.start: 10.0, c2.start: 2.0}, {c1.start}, UTC)
    assert r.months[0].drain_kwh == pytest.approx(2 * 50 / 50)  # 2 % at 1 kWh per %


def test_zero_kwh_trip_costs_nothing_and_spent_counts_all_money():
    c = Charge(start=T0, end=T0 + 1, kwh=50.0)
    c_no_kwh = Charge(start=T0 + 2, end=T0 + 3, kwh=None)
    t = Trip(start=T0 + 10, end=T0 + 20, kwh=0.0)
    r = build([t], [c, c_no_kwh], [], [], UTC)
    price.apply(r, {c.start: 10.0, c_no_kwh.start: 1.0}, set(), UTC)
    assert r.trips[0].cost_eur == 0.0
    assert r.journeys[0].cost_eur == 0.0
    assert r.months[0].spent_eur == pytest.approx(11.0)


def test_a_soc_gain_is_unknown():
    p = price.Pool(known_kwh=10.0, known_eur=2.0)
    p.scale_to(20.0)
    assert (p.known_kwh, p.known_eur, p.unknown_kwh) == (10.0, 2.0, 10.0)


def test_only_receipts_and_answers_are_money_paid(tmp_path):
    db = places.open_store(tmp_path)
    rows = [
        (1, "receipt", 5.0, 0),
        (2, "answer", 4.0, 0),
        (3, "estimate", 3.0, 0),
        (4, "receipt", 2.0, 1),
    ]
    for cid, src, cost, at_home in rows:
        db.execute(
            "INSERT INTO charges (id, end_ts, battery_kwh, type, at_home, match, price_source,"
            " cost_eur) VALUES (?, 0, 10, 'DC', ?, 'ok', ?, ?)",
            (cid, at_home, src, cost),
        )
    assert charges.known_costs(db) == {1: 5.0, 2: 4.0}
