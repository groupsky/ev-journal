from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from ev_journal import places, plugcheck
from ev_journal.settings import Settings

TZ = ZoneInfo("Europe/Sofia")
ADVICE = {"day": "2026-01-11", "level": "low", "message": "Утре: около 7 kWh."}
ARRIVED = 1_000_000


def check(advice=ADVICE, at_home=True, frames=(), meter_w=()):
    return plugcheck.decide(advice, at_home, plugcheck.plug_state(list(frames), list(meter_w)))


def test_cable_out_frame_sends_a_notice():
    text, _ = check(frames=[(ARRIVED + 60_000, True), (ARRIVED + 120_000, False)])
    assert text == f"{plugcheck.TEXT_OUT} {ADVICE['message']}"


def test_cable_in_frame_after_arrival_sends_nothing():
    text, why = check(frames=[(ARRIVED + 60_000, False), (ARRIVED + 120_000, True)])
    assert text is None and "plugged" in why


def test_charger_power_since_arrival_sends_nothing():
    text, _ = check(frames=[(ARRIVED + 60_000, False)], meter_w=[None, 40.0, 3_500.0])
    assert text is None


def test_idle_meter_alone_is_not_plugged():
    assert plugcheck.plug_state([], [40.0, 0.0]) == "unknown"


def test_no_frame_after_arrival_sends_the_unknown_text():
    text, _ = check()
    assert text == f"{plugcheck.TEXT_UNKNOWN} {ADVICE['message']}"


def test_car_not_at_home_or_place_unknown_sends_nothing():
    assert check(at_home=False)[0] is None


@pytest.mark.parametrize("advice", [None, {**ADVICE, "level": None}])
def test_no_charge_needed_sends_nothing(advice):
    assert check(advice=advice)[0] is None


def test_one_notice_per_evening_also_after_a_restart(tmp_path):
    sent = []
    n = plugcheck.notice("2026-01-10", "2026-01-10T22:30:00+02:00", "x")
    assert n["id"] == "plug_check:2026-01-10"
    db = places.open_store(tmp_path)
    assert plugcheck.send(db, n, sent.append, 1)
    assert not plugcheck.send(db, n, sent.append, 2)  # a second run
    db.close()
    db = places.open_store(tmp_path)  # a restart
    assert not plugcheck.send(db, n, sent.append, 3)
    assert plugcheck.send(db, plugcheck.notice("2026-01-11", "…", "x"), sent.append, 4)
    assert [m["id"] for m in sent] == ["plug_check:2026-01-10", "plug_check:2026-01-11"]


def test_a_failed_publish_is_not_stored(tmp_path):
    def fail(_n):
        raise RuntimeError("broker down")

    db = places.open_store(tmp_path)
    n = plugcheck.notice("2026-01-10", "…", "x")
    with pytest.raises(RuntimeError):
        plugcheck.send(db, n, fail, 1)
    assert plugcheck.send(db, n, lambda _n: None, 2)


def ms(*a):
    return int(datetime(*a, tzinfo=TZ).timestamp() * 1000)


def test_next_check_time():
    assert plugcheck.next_at(ms(2026, 1, 10, 18, 0), "22:30", TZ) == ms(2026, 1, 10, 22, 30)
    assert plugcheck.next_at(ms(2026, 1, 10, 22, 30), "22:30", TZ) == ms(2026, 1, 11, 22, 30)
    # Across the change to summer time.
    assert plugcheck.next_at(ms(2026, 3, 29, 1, 0), "22:30", TZ) == ms(2026, 3, 29, 22, 30)


def test_plug_check_setting():
    assert Settings.from_env({}).plug_check_at == "22:30"
    assert Settings.from_env({"EVJ_PLUG_CHECK_AT": "21:15"}).plug_check_at == "21:15"
    assert Settings.from_env({"EVJ_PLUG_CHECK_AT": "none"}).plug_check_at is None
    for bad in ("25:00", "late", ""):
        with pytest.raises(ValueError):
            Settings.from_env({"EVJ_PLUG_CHECK_AT": bad})
