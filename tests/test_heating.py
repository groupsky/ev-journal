from datetime import datetime
from zoneinfo import ZoneInfo

from ev_journal import heating
from ev_journal.heating import Preheat

MIN = 60_000
DAY = 86_400_000
TZ = ZoneInfo("Europe/Sofia")
# 2026-01-15 05:00 local: inside the winter night zone (22:00-06:00).
T0 = int(datetime(2026, 1, 15, 5, 0, tzinfo=TZ).timestamp() * 1000)


def minutes(spec, t=T0, e=100.0):
    """spec: list of (minutes, watts). 1-min meter rows with a running total."""
    rows = []
    for n, w in spec:
        for _ in range(n):
            e += w / 1000 / 60
            rows.append((t, float(w), e))
            t += MIN
    return rows


def test_one_preheat_from_state_frames():
    f = [(T0 + i * 4 * MIN - 1000, "plugged-preheat") for i in range(5)]
    f += [(T0 - 10 * MIN, "plugged"), (T0 + 60 * MIN, "plugged-preheat")]
    ps = heating.from_states(f)
    # Gaps under 5 min join; the frame 44 min later is a new session.
    assert [(p.start, p.end, p.source) for p in ps] == [
        (T0 - 1000, T0 + 16 * MIN - 1000, "state"),
        (T0 + 60 * MIN, T0 + 60 * MIN, "state"),
    ]


def test_meter_fallback():
    # The charge ends at T0; 30 min idle, then 20 min at 4 kW, then a trip.
    rows = minutes([(30, 0), (20, 4000), (30, 0)])
    pre_start = T0 + 30 * MIN
    soc = [(T0 - MIN, 80.0), (pre_start + 5 * MIN, 80.0)]
    ps = heating.from_meter(rows, [T0], [T0 + 70 * MIN], soc, [])
    assert [(p.start, p.end, p.source) for p in ps] == [(pre_start, pre_start + 19 * MIN, "meter")]
    # Not when the SoC rose (a charge), not when a trip came between, not
    # when the car app already gave a preheat at that time.
    assert (
        heating.from_meter(rows, [T0], [T0 + 70 * MIN], [(T0, 80.0), (pre_start, 81.0)], []) == []
    )
    assert heating.from_meter(rows, [T0], [T0 + 10 * MIN], soc, []) == []
    known = [Preheat(pre_start + 2 * MIN, pre_start + 15 * MIN, "state")]
    assert heating.from_meter(rows, [T0], [T0 + 70 * MIN], soc, known) == []
    # Not before any charge has ended.
    assert heating.from_meter(rows, [T0 + 90 * MIN], [], soc, []) == []


def test_wall_and_battery_kwh():
    rows = minutes([(10, 0), (30, 6000), (10, 0)])
    p = Preheat(T0 + 10 * MIN, T0 + 40 * MIN, "state")
    assert abs(heating.wall_kwh(rows, p) - 3.0) < 1e-9
    counters = [(T0, 1000.0, 900.0), (T0 + 20 * MIN, 1000.5, 900.1), (T0 + 40 * MIN, 1001.0, 900.2)]
    assert abs(heating.battery_kwh(counters, p) - 0.8) < 1e-9
    assert heating.battery_kwh([], p) is None


def test_day_and_night_price():
    now = T0
    rows = [
        {
            "time": now - DAY,
            "cost_day_eur": 2.0,
            "meter_kwh_day": 10.0,
            "cost_night_eur": 1.0,
            "meter_kwh_night": 10.0,
        },
        {
            "time": now - 2 * DAY,
            "cost_day_eur": 4.0,
            "meter_kwh_day": 10.0,
            "cost_night_eur": None,
            "meter_kwh_night": 0.0,
        },
        {"time": now - 40 * DAY, "cost_day_eur": 99.0, "meter_kwh_day": 1.0},  # too old
    ]
    prices = heating.home_prices(rows, now)
    assert prices == {"day": 0.3, "night": 0.1}
    assert heating.zone(T0, TZ) == "night"
    assert heating.zone(T0 + 2 * 3_600_000, TZ) == "day"  # 07:00
    p = Preheat(T0, T0 + 30 * MIN, "state", wall_kwh=3.0, battery_kwh=0.5, zone="night")
    assert abs(heating.cost(p, prices, 0.2) - (0.3 + 0.1)) < 1e-9
    p.zone = "day"
    assert abs(heating.cost(p, prices, 0.2) - (0.9 + 0.1)) < 1e-9
    assert heating.cost(p, prices, None) is None  # battery kWh with no pool price
    p.battery_kwh = 0.0
    assert abs(heating.cost(p, prices, None) - 0.9) < 1e-9


def test_journey_link():
    ps = [Preheat(T0, T0 + 20 * MIN, "state"), Preheat(T0 + 5 * DAY, T0 + 5 * DAY, "meter")]
    heating.link(ps, [T0 - DAY, T0 + 25 * MIN, T0 + DAY])
    assert [p.journey for p in ps] == [T0 + 25 * MIN, None]


def test_preheat_inside_a_home_charge_is_not_counted_twice():
    prices = {"night": 0.1, "day": 0.3}
    inside = Preheat(T0, T0 + 20 * MIN, "state", wall_kwh=2.0, zone="night")
    after = Preheat(
        T0 + 3 * 3_600_000, T0 + 3 * 3_600_000 + 20 * MIN, "state", wall_kwh=2.0, zone="day"
    )
    heating.inside_charge([inside, after], [(T0 - 3_600_000, T0 + 30 * MIN)])
    assert inside.in_charge == T0 - 3_600_000 and after.in_charge is None
    costs = {T0 - 3_600_000: 1.5}
    heating.charge_costs(costs, [inside, after], prices)
    # The charge keeps only its own money; the preheat's 0.2 € moves to heating.
    assert abs(costs[T0 - 3_600_000] - 1.3) < 1e-9


def test_compare_bands_groups_and_minimum():
    def j(c, pre, n):
        return [
            {
                "outdoor_c": c,
                "preheat": pre,
                "preheat_kwh": 2.0 if pre else 0.0,
                "battery_kwh": 4.0,
                "eur": 1.0,
                "km": 20.0,
            }
            for _ in range(n)
        ]

    rows = (
        j(-3.0, True, 3)
        + j(-1.0, False, 2)
        + j(2.0, False, 4)
        + [{"outdoor_c": None, "preheat": True}]
    )
    out = heating.compare(rows)
    assert [(g["band_c"], g["preheat"], g["count"]) for g in out] == [
        ([-5, 0], False, 2),
        ([-5, 0], True, 3),
        ([0, 5], False, 4),
    ]
    # Fewer than 3: the count and no means.
    assert "battery_kwh" not in out[0]
    assert out[1]["preheat_kwh"] == 2.0 and out[1]["eur"] == 1.0
    assert out[2]["kwh_per_100km"] == 20.0


def test_totals_and_journey_rows():
    from ev_journal.journeys import Journey, Month, Result, Trip

    j1 = Journey(trips=[Trip(T0 + 30 * MIN, T0 + 60 * MIN, kwh=4.0, km_homy=20.0)], stops=[])
    j2 = Journey(trips=[Trip(T0 + DAY, T0 + DAY + 30 * MIN, kwh=5.0, km_homy=20.0)], stops=[])
    for j in (j1, j2):
        j.label, j.trips[0].km, j.trips[0].cost_eur = "commute", 20.0, 1.0
    m = Month(start=T0 - 14 * DAY, key="2026-01")
    r = Result(trips=[], journeys=[j1, j2], outings=[], months=[m])
    p = Preheat(T0, T0 + 20 * MIN, "state", wall_kwh=2.0, cost_eur=0.2, journey=j1.id)
    heating.totals(r, [p], TZ)
    assert (j1.preheat_kwh, j1.preheat_eur, j2.preheat_kwh) == (2.0, 0.2, 0.0)
    assert (m.preheat_kwh, m.preheat_eur) == (2.0, 0.2)
    rows = heating.journey_rows(r, [(T0, -2.0), (T0 + DAY - 2 * 3_600_000, 3.0)])
    assert [(x["outdoor_c"], x["preheat"], x["battery_kwh"]) for x in rows] == [
        (-2.0, True, 4.0),
        (None, False, 5.0),  # the last reading is older than an hour
    ]
    assert abs(rows[0]["eur"] - 1.2) < 1e-9
