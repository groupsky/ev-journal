from zoneinfo import ZoneInfo

from week import PLACES, week

from ev_journal import outputs
from ev_journal.geo import dist_m
from ev_journal.journeys import build
from ev_journal.track import journey_track

UTC = ZoneInfo("UTC")
S = 1000


def line_fixes(n: int, step_m: float, every_ms: int = S) -> list:
    """n fixes going north from a made-up place at sea, step_m apart."""
    deg = step_m / 111_195
    return [(k * every_ms, 0.1 + k * deg, 0.2) for k in range(n)]


def test_thinning_matches_ev_track():
    r = build(week().trips, week().charges, week().fixes, PLACES, UTC)
    j = r.journeys[0]
    rows = [x for x in outputs.influx_lines(r) if x.startswith(f"ev_track,journey={j.id} ")]
    pts = journey_track([f for t in j.trips for f in t.fixes], [])
    assert len(pts) == len(rows) > 2
    for p, row in zip(pts, rows, strict=True):
        assert row.endswith(f" {p['t']}") and f"lat={float(p['lat'])}" in row


def test_kwh_per_100km_per_part():
    # 2 km at 10 m per second and 15 kWh/100 km: the net counter (out - in)
    # grows 0.3 kWh in 0.1 kWh steps. cum_in grows too (regen) and counts against.
    fixes = line_fixes(201, 10.0)
    readings = [
        (k * S, 100.0 + (k * 15 // 1000) / 10 + k * 0.001, 50.0 + k * 0.001) for k in range(201)
    ]
    pts = journey_track(fixes, readings)
    assert pts[0]["kwh_per_100km"] is None
    parts = [
        (p["kwh_per_100km"], dist_m(a["lat"], a["lon"], p["lat"], p["lon"]))
        for a, p in zip(pts, pts[1:], strict=False)
    ]
    assert all(r is not None for r, _ in parts)
    kwh = sum(r * m / 100_000 for r, m in parts)
    assert abs(kwh - 0.3) < 1e-6
    # Each 0.1 kWh step is spread over the metres that used it: no part jumps
    # to a whole step over 50 m (200 kWh/100 km).
    assert all(10 < r < 20 for r, _ in parts)


def test_null_where_the_counters_have_a_gap():
    # 10 m per second and 0.01 kWh per second: 100 kWh/100 km.
    # No readings from 30 s to 100 s (a gap), and none after 100 s.
    fixes = line_fixes(121, 10.0)
    readings = [(k * S, 100.0 + k * 0.01, 50.0) for k in range(101) if k <= 30 or k == 100]
    pts = journey_track(fixes, readings)
    for p in pts[1:]:
        if p["t"] <= 30 * S:
            assert abs(p["kwh_per_100km"] - 100) < 0.1
        else:  # across the gap, or after the last reading
            assert p["kwh_per_100km"] is None
    assert any(p["t"] > 100 * S for p in pts)


def test_no_fixes_no_points():
    assert journey_track([], [(0, 1.0, 0.0)]) == []
