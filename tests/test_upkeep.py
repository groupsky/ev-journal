"""Upkeep status with made-up items (#44)."""

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest
from conftest import TOKEN, CheckedClient

from ev_journal import places, source
from ev_journal.api import create_app
from ev_journal.upkeep import add_months, derive, mqtt_part, urgency

TODAY = date(2030, 6, 15)


def item(**kw):
    base = {
        "id": "x",
        "name": "Test item",
        "kind": "service",
        "due_date": None,
        "every_months": None,
        "every_km": None,
        "last_date": None,
        "last_km": None,
        "remind_days": [30, 7],
        "remind_km": 1000,
        "note": None,
    }
    return {**base, **kw}


def test_add_months_clamps_the_day():
    assert add_months(date(2030, 1, 31), 1) == date(2030, 2, 28)
    assert add_months(date(2030, 11, 5), 14) == date(2032, 1, 5)


def test_paper_by_date():
    d = derive(item(kind="paper", due_date="2030-09-01"), TODAY, 50_000)
    assert d["next_date"] == "2030-09-01" and d["days_left"] == 78
    assert d["next_km"] is None and d["km_left"] is None and d["status"] == "ok"


def test_paper_soon_at_30_days_and_overdue():
    assert derive(item(kind="paper", due_date="2030-07-15"), TODAY, None)["status"] == "soon"
    assert derive(item(kind="paper", due_date="2030-07-16"), TODAY, None)["status"] == "ok"
    d = derive(item(kind="paper", due_date="2030-06-14"), TODAY, None)
    assert d["days_left"] == -1 and d["status"] == "overdue"


def test_service_by_km_only():
    d = derive(item(every_km=15_000, last_km=40_000), TODAY, 50_000)
    assert d["next_date"] is None and d["days_left"] is None
    assert d["next_km"] == 55_000 and d["km_left"] == 5_000 and d["status"] == "ok"
    assert derive(item(every_km=15_000, last_km=40_000), TODAY, 54_000)["status"] == "soon"
    assert derive(item(every_km=15_000, last_km=40_000), TODAY, 53_999)["status"] == "ok"
    assert derive(item(every_km=15_000, last_km=40_000), TODAY, 55_001)["status"] == "overdue"


def test_service_by_months_only():
    d = derive(item(every_months=24, last_date="2028-08-01"), TODAY, 50_000)
    assert d["next_date"] == "2030-08-01" and d["days_left"] == 47
    assert d["next_km"] is None and d["status"] == "ok"


def test_service_by_both_whichever_comes_first():
    # The date is far; the km is close: soon.
    d = derive(
        item(every_months=24, last_date="2030-01-01", every_km=30_000, last_km=20_000),
        TODAY,
        49_500,
    )
    assert d["days_left"] > 30 and d["km_left"] == 500 and d["status"] == "soon"
    # The km are far; the date is past: overdue.
    d = derive(
        item(every_months=12, last_date="2029-06-01", every_km=30_000, last_km=40_000),
        TODAY,
        41_000,
    )
    assert d["km_left"] == 29_000 and d["days_left"] < 0 and d["status"] == "overdue"


def test_no_odometer_yet_the_date_still_works():
    d = derive(
        item(every_months=12, last_date="2029-07-01", every_km=15_000, last_km=40_000), TODAY, None
    )
    assert d["next_km"] == 55_000 and d["km_left"] is None
    assert d["days_left"] == 16 and d["status"] == "soon"


def test_order_and_mqtt_part():
    ds = [
        derive(item(id=str(i), kind="paper", due_date=f"2031-0{i}-01"), TODAY, None)
        for i in range(1, 6)
    ]
    ds.append(derive(item(id="late", kind="paper", due_date="2030-01-01"), TODAY, None))
    ds.sort(key=urgency)
    assert [d["id"] for d in ds] == ["late", "1", "2", "3", "4", "5"]
    part = mqtt_part(ds)
    assert [p["next_date"] for p in part] == [
        "2030-01-01",
        "2031-01-01",
        "2031-02-01",
        "2031-03-01",
    ]
    assert set(part[0]) == {"name", "status", "next_date", "next_km", "days_left", "km_left"}


# The API, with a made-up odometer.

PAPER = {"name": "Test paper", "kind": "paper", "due_date": "2999-01-01"}
OIL = {"name": "Test oil", "kind": "service", "every_km": 15000, "last_km": 40000}


@pytest.fixture
def odo():
    return {"value": (1_900_000_000_000, 50_000.0)}


@pytest.fixture
def upk(data_dir, odo):
    app = create_app(
        places.store_path(data_dir), TOKEN, ZoneInfo("UTC"), km_now=lambda: odo["value"]
    )
    c = CheckedClient(app)
    c.headers["Authorization"] = f"Bearer {TOKEN}"
    return c


def test_api_create_list_change_delete(upk):
    pid = upk.post("/upkeep", json=PAPER).json()["id"]
    oid = upk.post("/upkeep", json={**OIL, "last_km": 36000}).json()["id"]
    got = upk.get("/upkeep").json()
    assert got["km_now"] == 50_000 and got["km_now_time"] == 1_900_000_000_000
    assert [i["id"] for i in got["items"]] == [oid, pid]  # soon before ok
    assert got["items"][0]["status"] == "soon" and got["items"][0]["km_left"] == 1000
    w = upk.put(f"/upkeep/{oid}", json={**OIL, "version": 1}).json()
    assert w["version"] == 2
    assert upk.get(f"/upkeep/{oid}").json()["status"] == "ok"
    upk.delete(f"/upkeep/{oid}")
    assert upk.get(f"/upkeep/{oid}").status_code == 404
    assert [i["id"] for i in upk.get("/upkeep").json()["items"]] == [pid]


def test_api_no_odometer_yet(upk, odo):
    odo["value"] = None
    oid = upk.post("/upkeep", json={**OIL, "every_months": 12, "last_date": "2999-01-01"}).json()
    got = upk.get("/upkeep").json()
    assert got["km_now"] is None
    item = got["items"][0]
    assert item["id"] == oid["id"] and item["km_left"] is None and item["next_date"] == "3000-01-01"
    assert item["status"] == "ok"


def test_api_done_moves_last_and_logs(upk):
    oid = upk.post("/upkeep", json=OIL).json()["id"]
    got = upk.post(f"/upkeep/{oid}/done", json={"note": "test"}).json()
    assert got["last_km"] == 50_000 and got["next_km"] == 65_000 and got["version"] == 2
    assert got["last_date"] == datetime.now(ZoneInfo("UTC")).date().isoformat()
    got = upk.post(f"/upkeep/{oid}/done", json={"date": "2999-02-01", "km": 60000}).json()
    assert got["last_date"] == "2999-02-01" and got["last_km"] == 60_000
    log = upk.get(f"/upkeep/{oid}/log").json()
    assert [x["km"] for x in log] == [60_000, 50_000] and log[1]["note"] == "test"


def test_api_paper_done_needs_due_date(upk):
    pid = upk.post("/upkeep", json=PAPER).json()["id"]
    assert upk.post(f"/upkeep/{pid}/done", json={}).status_code == 422
    assert upk.get(f"/upkeep/{pid}/log").json() == []
    got = upk.post(f"/upkeep/{pid}/done", json={"due_date": "3000-01-01"}).json()
    assert got["due_date"] == "3000-01-01" and got["next_date"] == "3000-01-01"
    assert upk.get(f"/upkeep/{pid}/log").json()[0]["due_date"] == "3000-01-01"
    assert upk.post(f"/upkeep/{pid}/done", json={"due_date": "soon"}).status_code == 422
    assert upk.post(f"/upkeep/{pid}/done", json={"due_date": "30000101"}).status_code == 422
    assert upk.post("/upkeep/nope/done", json={}).status_code == 404


class FakeInflux:
    def __init__(self, rows):
        self.rows, self.queries = rows, []

    def query(self, q):
        self.queries.append(q)
        return self.rows


def test_km_now_from_the_odometer():
    db = FakeInflux([{"time": 1_900_000_000_000, "last": 50_000}])
    assert source.last_odometer(db) == (1_900_000_000_000, 50_000.0)
    assert "'odometer'" in db.queries[0] and "ioniq" in db.queries[0]
    assert source.last_odometer(FakeInflux([])) is None


def test_api_odometer_fails_or_missing_keeps_km(upk, odo, data_dir):
    oid = upk.post("/upkeep", json=OIL).json()["id"]

    def broken():
        raise RuntimeError("no influx")

    odo["value"] = None
    got = upk.post(f"/upkeep/{oid}/done", json={}).json()
    assert got["last_km"] == 40_000 and got["next_km"] == 55_000 and got["km_left"] is None
    assert upk.get(f"/upkeep/{oid}/log").json()[0]["km"] is None
    app = create_app(places.store_path(data_dir), TOKEN, ZoneInfo("UTC"), km_now=broken)
    c = CheckedClient(app)
    c.headers["Authorization"] = f"Bearer {TOKEN}"
    got = c.get("/upkeep").json()
    assert got["km_now"] is None and got["items"][0]["km_left"] is None
