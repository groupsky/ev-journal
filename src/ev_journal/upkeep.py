"""Car upkeep: papers that run out on a date, and service items due by
months or by km, whichever comes first (#44). See docs/design.md, "Upkeep".

The items live in the same SQLite file as places, with the same sync rules.
"""

import calendar
import json
import sqlite3
from datetime import date

SCHEMA = """
CREATE TABLE IF NOT EXISTS upkeep_items (id TEXT PRIMARY KEY, name TEXT NOT NULL,
  kind TEXT NOT NULL, due_date TEXT, every_months INTEGER, every_km REAL,
  last_date TEXT, last_km REAL, remind_days TEXT NOT NULL DEFAULT '[30, 7]',
  remind_km REAL NOT NULL DEFAULT 1000, note TEXT,
  version INTEGER NOT NULL, updated_at INTEGER NOT NULL,
  deleted INTEGER NOT NULL DEFAULT 0, seq INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS upkeep_log (id INTEGER PRIMARY KEY AUTOINCREMENT,
  item_id TEXT NOT NULL, date TEXT NOT NULL, km REAL, note TEXT, due_date TEXT,
  created_at INTEGER NOT NULL);
"""

RANK = {"overdue": 0, "soon": 1, "ok": 2}


def init(db: sqlite3.Connection) -> None:
    db.executescript(SCHEMA)


def add_months(d: date, n: int) -> date:
    """The same day n months later; the 31st becomes the month's last day."""
    y, m = divmod(d.month - 1 + n, 12)
    y, m = d.year + y, m + 1
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def _day(s: str | None) -> date | None:
    return date.fromisoformat(s) if s else None


def derive(item: dict, today: date, km_now: float | None) -> dict:
    """The item with next_date, next_km, days_left, km_left and status.

    item has the columns of upkeep_items (remind_days as a list).
    """
    if item["kind"] == "paper":
        next_date = _day(item["due_date"])
        next_km = None
    else:
        last = _day(item["last_date"])
        months = item["every_months"]
        next_date = add_months(last, months) if last and months else None
        lk, every = item["last_km"], item["every_km"]
        next_km = lk + every if lk is not None and every else None
    days_left = (next_date - today).days if next_date else None
    km_left = round(next_km - km_now, 1) if next_km is not None and km_now is not None else None
    remind = item["remind_days"]
    if (days_left is not None and days_left < 0) or (km_left is not None and km_left < 0):
        status = "overdue"
    elif (days_left is not None and remind and days_left <= remind[0]) or (
        km_left is not None and km_left <= item["remind_km"]
    ):
        status = "soon"
    else:
        status = "ok"
    return {
        **item,
        "next_date": next_date.isoformat() if next_date else None,
        "next_km": next_km,
        "days_left": days_left,
        "km_left": km_left,
        "status": status,
    }


def urgency(d: dict) -> tuple:
    """Sort key: overdue, then soon, then ok; inside each, the nearest first."""
    inf = float("inf")
    days = d["days_left"] if d["days_left"] is not None else inf
    km = d["km_left"] if d["km_left"] is not None else inf
    return (RANK[d["status"]], days, km, d["name"], d["id"])


def row_dict(r: sqlite3.Row) -> dict:
    d = dict(r)
    d["remind_days"] = json.loads(d["remind_days"])
    return d


def items(db: sqlite3.Connection, today: date, km_now: float | None) -> list[dict]:
    """All live items with derived values, the most urgent first."""
    rows = db.execute("SELECT * FROM upkeep_items WHERE deleted = 0")
    return sorted((derive(row_dict(r), today, km_now) for r in rows), key=urgency)


def mqtt_part(all_items: list[dict]) -> list[dict]:
    """For the retained summary: every soon or overdue item, and the next 3 others."""
    keys = ("name", "status", "next_date", "next_km", "days_left", "km_left")
    urgent = [d for d in all_items if d["status"] != "ok"]
    rest = [d for d in all_items if d["status"] == "ok"][:3]
    return [{k: d[k] for k in keys} for d in urgent + rest]
