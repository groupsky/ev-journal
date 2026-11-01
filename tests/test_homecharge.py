import json
from zoneinfo import ZoneInfo

from week import PLACES, week

from ev_journal import homecharge, outputs
from ev_journal.homecharge import HomeCharge, Model, blocks, drift, fit, live, split
from ev_journal.journeys import build

MIN = 60_000
T0 = 1_768_000_000_000


def minutes(spec):
    """spec: list of (minutes, watts). Returns 1-min rows with a running meter total."""
    rows, t, e = [], T0, 100.0
    for n, w in spec:
        for _ in range(n):
            e += w / 1000 / 60
            rows.append((t, float(w), round(e, 2)))
            t += MIN
    return rows


def test_blocks_and_preheat():
    rows = minutes([(10, 0), (120, 1220), (1, 0), (60, 1220), (300, 0), (30, 1300), (10, 0)])
    bs = blocks(rows)
    # The 1 min pause stays inside the block; the 30 min block later is preheat.
    assert len(bs) == 2
    assert bs[0].end - bs[0].start == 180 * MIN
    charging, preheat = split(bs)
    assert abs(charging - 1.22 * 3) < 0.03
    assert abs(preheat - 0.65) < 0.03


def test_a_long_later_block_is_charging():
    bs = blocks(minutes([(60, 1220), (60, 0), (50, 1220)]))
    charging, preheat = split(bs)
    assert preheat == 0
    assert abs(charging - 1.22 * 110 / 60) < 0.03


def charges(n=20, ratio=0.9, overhead=1.0, kpp=0.30):
    out = []
    for i in range(n):
        meter = 4 + i % 10
        bat = ratio * (meter - overhead)
        out.append(HomeCharge(T0 + i * 86_400_000, meter, bat, 30, 30 + bat / kpp, "2026-01"))
    return out


def test_fit_and_backtest():
    m = fit(charges())
    assert abs(m.ratio - 0.9) < 1e-9
    assert abs(m.overhead - 1.0) < 1e-9
    assert abs(m.kwh_per_pct - 0.30) < 1e-9
    errors = homecharge.backtest(charges())
    assert len(errors) == 10
    assert max(errors) < 1e-6


def test_soc_stops_at_100():
    assert Model().soc(90, 50) == 100


def month(key, kpp):
    return [
        HomeCharge(T0, 10 + i, 0.9 * (9 + i), 20, 20 + 0.9 * (9 + i) / kpp, key) for i in range(3)
    ]


def test_drift_flag_on_a_5_pct_move():
    hc = month("2026-01", 0.30) + month("2026-02", 0.30) + month("2026-03", 0.30)
    hc += month("2026-04", 0.315)  # +5 %
    d = drift(hc, limit_pct=3, limit_kwh=0.5)
    assert d["2026-03"].drift_flag == ""
    assert d["2026-04"].drift_flag == "kwh_per_pct +5.0 %"
    assert abs(d["2026-04"].kwh_per_pct - 0.315) < 1e-9
    assert abs(d["2026-04"].meter_ratio - 0.9 * 30 / 33) < 1e-9  # battery ÷ meter kWh
    # A 2 % move stays under the limit.
    hc = month("2026-01", 0.30) + month("2026-02", 0.306)
    assert drift(hc, 3, 0.5)["2026-02"].drift_flag == ""


def test_live():
    m = Model(ratio=0.9, overhead=1.0, kwh_per_pct=0.3)
    bs = blocks(minutes([(10, 0), (120, 1200)]))
    now = bs[-1].end + MIN
    h = live(bs, 30.0, m, now, 80)
    assert h["running"]
    assert abs(h["meter_kwh"] - 2.4) < 0.02
    assert abs(h["soc_now"] - (30 + 1.4 * 0.9 / 0.3)) < 0.1
    need = (80 - h["soc_now"]) * 0.3 / 0.9
    assert abs(h["meter_kwh_to_target"] - need) < 1e-9
    assert abs(h["min_to_target"] - need / 1.2 * 60) < 1e-6
    # Done: no time to the target.
    assert live(bs, 30.0, m, now + 60 * MIN, 80)["min_to_target"] is None
    assert live([], 30.0, m, now, 80) is None
    assert live(bs, None, m, now, 80) is None


def test_live_holds_a_short_later_block():
    m = Model(ratio=0.9, overhead=1.0, kwh_per_pct=0.3)
    bs = blocks(minutes([(120, 1200), (60, 0), (20, 1200)]))
    h = live(bs, 30.0, m, bs[-1].end + MIN, 80)
    assert abs(h["meter_kwh"] - 2.4) < 0.02  # the 20 min block may be preheat


def test_outputs():
    w = week()
    r = build(w.trips, w.charges, w.fixes, PLACES, ZoneInfo("UTC"))
    r.months[0].drift_flag = "meter_ratio -3.5 %"
    r.months[0].meter_ratio = 0.8
    lines = [ln for ln in outputs.influx_lines(r) if ln.startswith("ev_month ")]
    assert 'drift_flag="meter_ratio -3.5 %"' in lines[0]
    assert "meter_ratio=0.8" in lines[0]
    s = outputs.summary(r, r.journeys[-1].end, ZoneInfo("UTC"))
    assert s["home_charge"] is None
    assert s["month"]["drift_flag"] == "meter_ratio -3.5 %"
    bs = blocks(minutes([(120, 1200)]))
    r.home_charge = live(bs, 30.0, Model(), bs[-1].end, 80)
    s = outputs.summary(r, r.journeys[-1].end, ZoneInfo("UTC"))
    json.dumps(s)
    assert s["home_charge"]["running"] is True
    assert s["home_charge"]["soc_plugin"] == 30.0
