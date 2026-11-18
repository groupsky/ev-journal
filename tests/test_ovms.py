"""OVMS export import (#62): synthetic zips only, made-up numbers."""

import io
import zipfile
from datetime import UTC, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from ev_journal import __main__ as m
from ev_journal import forecast, homecharge, ovms, places
from ev_journal.homecharge import HomeCharge
from ev_journal.journeys import build

S_HEAD = "rec_time,x,soc,chargestate,chargepower,idealrange\n"
D_HEAD = "rec_time,x,odometer,speed,tbattery,ambient\n"
L_HEAD = "rec_time,x,latitude,longitude,gpslock,speed\n"


def make_zip(path, s="", d="", l=""):  # noqa: E741
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("s.csv", S_HEAD + s)
        z.writestr("d.csv", D_HEAD + d)
        z.writestr("l.csv", L_HEAD + l)
    path.write_bytes(buf.getvalue())


def ms(y, mo, d, h, mi=0):
    return int(datetime(y, mo, d, h, mi, tzinfo=UTC).timestamp() * 1000)


def srow(t, soc, state="charging", kw=1.0):
    return f"{t},0,{soc},{state},{kw},{soc * 2}\n"


def night(day="2025-01-10", soc0=40, hours=6, kw=1.0, step=3.0):
    """A charge from 01:00 local, one row per hour."""
    rows = [srow(f"{day} {1 + h:02d}:00:00", soc0 + step * h, kw=kw) for h in range(hours + 1)]
    return "".join(rows) + srow(f"{day} {2 + hours:02d}:00:00", soc0 + step * hours, "stopped", 0)


def test_bms_conversion():
    # display = -1.51 + 49/46 × BMS, so BMS = (display + 1.51) / 1.065
    assert ovms.bms(50.0) == pytest.approx((50 + 1.51) / (49 / 46))
    assert ovms.bms(forecast.on_screen(70.0)) == pytest.approx(70.0)
    # A rate in %/h divides by the slope.
    assert ovms.bms(60.0) - ovms.bms(50.0) == pytest.approx(10 / (49 / 46))


def test_berlin_time_and_repeated_october_hour(tmp_path):
    # Winter: 01:00 Berlin = 00:00 UTC; summer: 02:00 Berlin = 00:00 UTC.
    make_zip(tmp_path / "a.zip", srow("2025-01-10 01:00:00", 50) + srow("2025-07-10 02:00:00", 51))
    rows = ovms.read_dir(tmp_path)["s"]
    assert [r["t"] for r in rows] == [ms(2025, 1, 10, 0), ms(2025, 7, 10, 0)]
    # 2025-10-26: 02:00–03:00 comes twice; the time going back sets fold=1.
    make_zip(
        tmp_path / "a.zip",
        srow("2025-10-26 02:10:00", 50)
        + srow("2025-10-26 02:50:00", 51)
        + srow("2025-10-26 02:20:00", 52)
        + srow("2025-10-26 02:50:00", 53)
        + srow("2025-10-26 03:10:00", 54),
    )
    rows = ovms.read_dir(tmp_path)["s"]
    assert [r["t"] for r in rows] == [
        ms(2025, 10, 26, 0, 10),
        ms(2025, 10, 26, 0, 50),
        ms(2025, 10, 26, 1, 20),
        ms(2025, 10, 26, 1, 50),
        ms(2025, 10, 26, 2, 10),
    ]
    assert [r["soc"] for r in rows] == ["50", "51", "52", "53", "54"]


def test_border_duplicates_and_zero_rows(tmp_path):
    make_zip(
        tmp_path / "2025-01-10.zip",
        srow("2025-01-10 23:50:00", 50) + srow("2025-01-11 00:50:00", 0),
        d="2025-01-10 23:50:00,0,1000,0,10,5\n2025-01-10 23:55:00,0,0,0,10,5\n",
    )
    make_zip(tmp_path / "2025-01-11.zip", srow("2025-01-10 23:50:00", 50))
    rows = ovms.read_dir(tmp_path)
    assert [r["soc"] for r in rows["s"]] == ["50"]  # one copy, soc 0 dropped
    assert [r["odometer"] for r in rows["d"]] == ["1000"]  # odometer 0 dropped


def charges_of(tmp_path, s):
    make_zip(tmp_path / "a.zip", s)
    return ovms.charges(ovms.read_dir(tmp_path)["s"])


def test_ghost_rows_with_no_power(tmp_path):
    ghost = srow("2025-01-10 04:00:00", 60, kw=0.2) + srow("2025-01-10 04:20:00", 60, kw=0.2)
    assert charges_of(tmp_path, ghost) == []


def test_span_of_a_charge(tmp_path):
    (c,) = charges_of(tmp_path, srow("2025-01-10 00:30:00", 40, "stopped", 0) + night())
    assert c.start == ms(2025, 1, 10, 0) and c.end == ms(2025, 1, 10, 6)
    assert c.hours == 6
    assert c.soc_start == pytest.approx(ovms.bms(40)) and c.soc_end == pytest.approx(ovms.bms(58))
    assert c.power_kw == pytest.approx(1.0)


def test_pause_under_3h_joins(tmp_path):
    s = (
        "".join(srow(f"2025-01-10 {h:02d}:00:00", 40 + 3 * (h - 1)) for h in (1, 2, 3))
        + srow("2025-01-10 03:30:00", 46, "stopped", 0)
        + "".join(srow(f"2025-01-10 {h:02d}:30:00", 46 + 3 * (h - 4)) for h in (4, 5, 6))
    )
    (c,) = charges_of(tmp_path, s)
    assert c.hours == 4  # the 1.5 h from 03:00 to 04:30 is left out
    assert c.soc_end - c.soc_start == pytest.approx(ovms.bms(52) - ovms.bms(40))


def test_pause_over_3h_does_not_join(tmp_path):
    s = (
        "".join(srow(f"2025-01-10 {h:02d}:00:00", 40 + 3 * (h - 1)) for h in (1, 2, 3))
        + srow("2025-01-10 03:30:00", 46, "stopped", 0)
        + "".join(srow(f"2025-01-10 {h:02d}:30:00", 46 + 3 * (h - 7)) for h in (7, 8, 9))
    )
    assert len(charges_of(tmp_path, s)) == 2


def test_load_home_meter_and_store(tmp_path):
    zips = tmp_path / "z"
    zips.mkdir()
    make_zip(
        zips / "a.zip",
        night(),
        d="2025-01-10 00:30:00,0,1000,0,8,-2\n",
        l="2025-01-10 00:30:00,0,0.1,0.2,1,0\n",
    )
    pl = [places.Place("h", "Home", "home", 0.1, 0.2, 100)]
    t0 = ms(2025, 1, 10, 0)

    def meter(start, end):  # 1.22 kW from t0 for 6 h
        return [
            (t0 + i * 60_000, 1220.0 * (i >= 0), 100 + max(i, 0) * 1.22 / 60)
            for i in range(-6, 361)
        ]

    (c,) = ovms.load(zips, pl, meter)
    assert c.at_home and c.ambient_c == -2 and c.battery_c == 8
    assert c.meter_kwh == pytest.approx(6 * 1.22, abs=0.05)
    db = places.open_store(tmp_path)
    ovms.save(db, [c])
    ovms.save(db, ovms.load(zips, pl, meter))  # again: same rows
    assert ovms.stored(db) == [c]
    (away,) = ovms.load(zips, [], meter)
    assert not away.at_home and away.meter_kwh is None


def hcharge(t, meter_kw, kw, hours, soc0=30.0, kpp=0.3):
    return HomeCharge(t, meter_kw * hours, kw * hours, soc0, soc0 + kw * hours / kpp, "", hours)


def ovms_charge(t, meter_kw, kw, hours, soc0=30.0, kpp=0.3):
    return ovms.Charge(
        t, t + int(hours * 3_600_000), hours, soc0, soc0 + kw * hours / kpp,
        at_home=True, meter_kwh=meter_kw * hours,
    )  # fmt: skip


DAY = 86_400_000


def live_charges():
    """Low and mid nights, and 3 short high top-ups (#60)."""
    t = ms(2026, 8, 1, 0)
    out = [hcharge(t + i * DAY, 1.22, 0.92, 6, kpp=0.3 + i * 0.001) for i in range(15)]
    out += [hcharge(t + (20 + i) * DAY, 1.93, 1.62, 5) for i in range(5)]
    return out + [hcharge(t + (30 + i) * DAY, 2.64, 1.95, 1.0) for i in range(3)]


def models(ov):
    s = SimpleNamespace(tz="UTC", drift_pct=5.0, drift_kwh=1.0)
    hc = live_charges()
    r = build([], [], [], [], ZoneInfo("UTC"))
    m.charge_models(s, r, hc, ov)
    return r, hc


def long_highs():
    t = ms(2025, 1, 1, 0)
    return [ovms_charge(t + i * DAY, 2.64, 2.35, 7) for i in range(6)]


def test_high_rate_comes_from_long_ovms_charges():
    r0, hc = models([])
    high0 = dict((n, kw) for n, _m, kw in r0.levels)["high"]
    r, _hc = models(long_highs())
    high = dict((n, kw) for n, _m, kw in r.levels)["high"]
    assert high == pytest.approx(2.35)
    assert high0 != pytest.approx(2.35)  # without them: scaled from mid
    assert len(r.level_sources["ovms"]) == 6
    assert "2025-01" in r.level_months  # they join the months too


def test_other_models_do_not_change(monkeypatch):
    r0, hc = models([])
    seen = []
    drift = homecharge.drift
    monkeypatch.setattr(homecharge, "drift", lambda cs, *a: seen.append(cs) or drift(cs, *a))
    ov = long_highs() + [ovms_charge(ms(2025, 2, 1, 0), 1.22, 0.8, 6, kpp=0.4)]
    r, _hc = models(ov)
    assert r.backtest == r0.backtest
    assert r.soc_model == r0.soc_model
    assert r.soc_model.kwh_per_pct == homecharge.kwh_per_pct(hc)
    assert seen == [hc]  # drift() sees the live charges only
    assert r.levels != r0.levels
