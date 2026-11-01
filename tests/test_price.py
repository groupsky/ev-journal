from zoneinfo import ZoneInfo

import pytest

from ev_journal import price
from ev_journal.journeys import Charge, Trip, build

UTC = ZoneInfo("UTC")
DAY = 86_400_000
T0 = 1_767_225_600_000  # 2026-01-01 00:00 UTC


def worked_example(away_cost=None):
    """The car arrives with all-unknown energy at 30 %. Three times: it charges
    30 -> 80 % with priced energy (50 kWh for 10 EUR), then drives back to 30 %.
    Each drive is 50 kWh and 250 km. kWh per % is 1 (a home charge teaches it).

    With away_cost, a first away charge (unknown unless its cost is given)
    brings it from 20 to 30 % before all that.
    """
    charges, trips, costs = [], [], {}
    if away_cost is not None:
        c0 = Charge(start=T0 - DAY, end=T0 - DAY + 3_600_000, kwh=10.0, soc_start=20, soc_end=30)
        charges.append(c0)
        if away_cost:
            costs[c0.start] = away_cost
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
    before, _ = worked_example(away_cost=0)
    late, _ = worked_example(away_cost=3.0)  # the receipt came later; a rerun
    assert before.trips[1].cost_eur is None
    assert late.trips[1].unknown_share < before.trips[1].unknown_share
    assert late.trips[2].cost_eur is not None
    assert late.trips[2].price_eur_per_kwh != pytest.approx(0.20)  # the receipt is in the mix


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
