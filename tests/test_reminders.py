"""Upkeep and tyre reminders with made-up items (#46)."""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from ev_journal import places, reminders
from ev_journal.tyres import Rules, overview
from ev_journal.upkeep import derive

TZ = ZoneInfo("Europe/Sofia")


def at(day: str, hour: int = 10) -> tuple[int, datetime]:
    local = datetime.combine(date.fromisoformat(day), datetime.min.time(), TZ).replace(hour=hour)
    return int(local.timestamp() * 1000), local


def item(**kw):
    base = {
        "id": "p1",
        "name": "Test paper",
        "kind": "paper",
        "due_date": "2030-07-15",
        "every_months": None,
        "every_km": None,
        "last_date": None,
        "last_km": None,
        "remind_days": [30, 7],
        "remind_km": 1000,
        "note": None,
    }
    return {**base, **kw}


def send(db, raw, day, hour=10, km=50_000, tyres=None):
    now, local = at(day, hour)
    items = [derive(i, local.date(), km) for i in raw]
    out = []
    reminders.run(db, items, tyres, now, local, out.append)
    return out


def test_30_day_and_7_day_notices_once_each(tmp_path):
    db = places.open_store(tmp_path)
    assert send(db, [item()], "2030-06-01") == []  # 44 days left
    [n] = send(db, [item()], "2030-06-15")
    assert n["id"] == "upkeep:p1:2030-07-15:30d" and n["kind"] == "upkeep"
    assert n["text"] == "Test paper: изтича след 30 дни (2030-07-15)."
    assert send(db, [item()], "2030-06-16") == []  # the next run
    [n] = send(db, [item()], "2030-07-08")
    assert n["id"] == "upkeep:p1:2030-07-15:7d"
    db.close()
    db = places.open_store(tmp_path)  # a restart
    assert send(db, [item()], "2030-07-09") == []


def test_created_late_sends_only_the_nearest_day_threshold(tmp_path):
    db = places.open_store(tmp_path)
    [n] = send(db, [item()], "2030-07-10")
    assert n["id"].endswith(":7d")
    assert send(db, [item()], "2030-07-11") == []


def test_1000_km_notice_once(tmp_path):
    svc = item(
        id="s1",
        name="Test fluid",
        kind="service",
        due_date=None,
        last_date="2030-01-01",
        every_months=24,
        last_km=40_000,
        every_km=20_000,
    )
    db = places.open_store(tmp_path)
    assert send(db, [svc], "2030-06-01", km=58_000) == []
    [n] = send(db, [svc], "2030-06-02", km=59_000)
    assert n["id"] == "upkeep:s1:60000:1000km"
    assert n["text"] == "Test fluid: след 1 000 km или на 2032-01-01, което дойде първо."
    assert send(db, [svc], "2030-06-03", km=59_500) == []


def test_overdue_repeats_weekly(tmp_path):
    db = places.open_store(tmp_path)
    [n] = send(db, [item()], "2030-07-16")  # a Tuesday
    assert n["id"] == "upkeep:p1:2030-07-15:overdue:2030-W29"
    assert n["text"] == "Test paper: просрочено (2030-07-15)."
    assert send(db, [item()], "2030-07-21") == []  # Sunday, same week
    [n] = send(db, [item()], "2030-07-22")
    assert n["id"].endswith(":overdue:2030-W30")


def test_overdue_by_km(tmp_path):
    svc = item(id="s1", kind="service", due_date=None, last_km=40_000, every_km=20_000)
    db = places.open_store(tmp_path)
    [n] = send(db, [svc], "2030-06-03", km=60_100)
    assert n["id"] == "upkeep:s1:60000:overdue:2030-W23"


def test_done_starts_a_new_cycle(tmp_path):
    svc = item(id="s1", kind="service", due_date=None, last_date="2029-08-01", every_months=12)
    db = places.open_store(tmp_path)
    [n] = send(db, [svc], "2030-07-02")
    assert n["id"] == "upkeep:s1:2030-08-01:30d"
    svc = {**svc, "last_date": "2030-07-03"}  # done
    assert send(db, [svc], "2030-07-04") == []
    [n] = send(db, [svc], "2031-06-03")
    assert n["id"] == "upkeep:s1:2031-07-03:30d"


def test_night_holds_a_notice_until_8(tmp_path):
    db = places.open_store(tmp_path)
    assert send(db, [item()], "2030-06-15", hour=21) == []
    assert send(db, [item()], "2030-06-16", hour=7) == []
    [n] = send(db, [item()], "2030-06-16", hour=8)
    assert n["id"].endswith(":30d")


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


SWAPS = [{"date": "2030-04-01", "km": 40_000.0, "set_id": "s"}]


def tyres(day, temp, sets=None):
    sets = sets or [tset("s", "summer"), tset("w", "winter")]
    return overview(sets, SWAPS, date.fromisoformat(day), 50_000, [temp] * 7, Rules())


def test_tyre_hint_first_then_every_14_days(tmp_path):
    db = places.open_store(tmp_path)
    [n] = send(db, [], "2030-11-01", tyres=tyres("2030-11-01", 3.0))
    assert n == {
        **n,
        "id": "tyre_hint:winter:2030-11-01",
        "kind": "tyre_hint",
        "text": "Гуми: време е за зимните.",
    }
    assert send(db, [], "2030-11-14", tyres=tyres("2030-11-14", 3.0)) == []
    [n] = send(db, [], "2030-11-15", tyres=tyres("2030-11-15", 3.0))
    assert n["id"] == "tyre_hint:winter:2030-11-15"


def test_tyre_hint_gone_and_back_is_new(tmp_path):
    db = places.open_store(tmp_path)
    assert len(send(db, [], "2030-11-01", tyres=tyres("2030-11-01", 3.0))) == 1
    assert send(db, [], "2030-11-03", tyres=tyres("2030-11-03", 12.0)) == []
    assert len(send(db, [], "2030-11-05", tyres=tyres("2030-11-05", 3.0))) == 1


def test_tyre_age_one_notice(tmp_path):
    sets = [tset("s", "summer", dot="0120"), tset("w", "winter", dot="0120", retired=1)]
    db = places.open_store(tmp_path)
    [n] = send(db, [], "2030-06-01", tyres=tyres("2030-06-01", 20.0, sets))
    assert n["id"] == "tyre_age:s" and n["kind"] == "tyre_age"
    assert send(db, [], "2030-06-02", tyres=tyres("2030-06-02", 20.0, sets)) == []


def test_pending_writes_nothing(tmp_path):
    db = places.open_store(tmp_path)
    now, local = at("2030-06-15")
    items = [derive(item(), local.date(), None)]
    assert len(reminders.pending(db, items, None, now, local)) == 1
    assert (
        len(reminders.pending(db, items, None, now + timedelta(hours=1).seconds * 1000, local)) == 1
    )


def test_send_reminders_publishes_on_the_notice_topic(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from ev_journal import __main__ as cli
    from ev_journal import mqtt

    db = places.open_store(tmp_path)
    db.execute(
        "INSERT INTO upkeep_items (id, name, kind, due_date, version, updated_at)"
        " VALUES ('p1', 'Test paper', 'paper', '2030-07-15', 1, 0)"
    )
    db.commit()
    db.close()
    sent = []
    monkeypatch.setattr(
        mqtt, "publish", lambda url, m, topic="", retain=True: sent.append((topic, retain, m))
    )
    monkeypatch.setattr(cli, "odometer_now", lambda s: None)
    monkeypatch.setattr(cli, "outside_temps", lambda s: [])
    s = SimpleNamespace(
        mqtt_url="mqtt://broker",
        data_dir=tmp_path,
        tz="Europe/Sofia",
        tyre_max_age_years=6.0,
        tyre_swap_c=7.0,
        tyre_swap_days=7,
    )
    now, _ = at("2030-06-15")
    cli.send_reminders(s, now)
    cli.send_reminders(s, now + 60_000)
    [(topic, retain, m)] = sent
    assert topic == "homy/ioniq/notice" and retain is False
    assert m["id"] == "upkeep:p1:2030-07-15:30d"
