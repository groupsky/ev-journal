"""Upkeep status with made-up items (#44)."""

from datetime import date

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
