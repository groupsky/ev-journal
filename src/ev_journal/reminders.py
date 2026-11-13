"""Upkeep and tyre reminders as notices on homy/ioniq/notice (#46).

Each threshold sends one notice: its id goes into the notices table and is
never sent again. Ids and texts: docs/outputs.md#notices.
"""

import sqlite3
from collections.abc import Callable
from datetime import datetime, timedelta

from ev_journal import plugcheck

FROM_HOUR, TO_HOUR = 8, 21  # notices go out from 08:00 to 21:00 local time
HINT_EVERY = timedelta(days=14)
SEASON = {"winter": "зимните", "summer": "летните"}


def _km(x: float) -> str:
    return f"{round(x):,}".replace(",", " ")


def _upkeep(d: dict, week: str) -> list[tuple[str, str]]:
    """(id, text) for the thresholds the item is past now. Only the nearest
    day threshold counts, so a later run does not send the farther one."""
    base = f"upkeep:{d['id']}"
    name, days, km = d["name"], d["days_left"], d["km_left"]
    if d["status"] == "overdue":
        if days is not None and days < 0:
            due, shown = d["next_date"], d["next_date"]
        else:
            due, shown = round(d["next_km"]), f"{_km(d['next_km'])} km"
        return [(f"{base}:{due}:overdue:{week}", f"{name}: просрочено ({shown}).")]
    out = []
    crossed = [n for n in d["remind_days"] if days is not None and days <= n]
    if crossed:
        n = min(crossed)
        if d["kind"] == "paper":
            text = f"{name}: изтича след {days} дни ({d['next_date']})."
        elif km is not None:
            text = (
                f"{name}: след {days} дни ({d['next_date']}) или {_km(km)} km, което дойде първо."
            )
        else:
            text = f"{name}: след {days} дни ({d['next_date']})."
        out.append((f"{base}:{d['next_date']}:{n}d", text))
    if km is not None and km <= d["remind_km"]:
        if d["next_date"]:
            text = f"{name}: след {_km(km)} km или на {d['next_date']}, което дойде първо."
        else:
            text = f"{name}: след {_km(km)} km."
        out.append((f"{base}:{round(d['next_km'])}:{round(d['remind_km'])}km", text))
    return out


def _last_hint(db: sqlite3.Connection) -> int | None:
    row = db.execute("SELECT MAX(time) FROM notices WHERE id LIKE 'tyre_hint:%'").fetchone()
    return row[0]


def pending(
    db: sqlite3.Connection, items: list[dict], tyres: dict | None, now: int, local: datetime
) -> list[dict]:
    """The notices due now that were not sent before. Writes nothing.

    items: upkeep.items(); tyres: tyres.overview() or None; now: ms; local: now
    in local time."""
    y, w, _ = local.isocalendar()
    week = f"{y}-W{w:02d}"
    found = [x for d in items for x in _upkeep(d, week)]
    if tyres:
        for s in tyres["sets"]:
            if s["age_warning"] and not s["retired"]:
                found.append(
                    (
                        f"tyre_age:{s['id']}",
                        f"Гуми {s['name']}: стари са, над {s['age_years']} години.",
                    )
                )
        h = tyres["hint"]
        last = _last_hint(db)
        if h and (last is None or now - last >= HINT_EVERY.total_seconds() * 1000):
            found.append(
                (f"tyre_hint:{h}:{local.date().isoformat()}", f"Гуми: време е за {SEASON[h]}.")
            )
    t = local.isoformat(timespec="seconds")
    out = []
    for nid, text in found:
        if not db.execute("SELECT 1 FROM notices WHERE id = ?", (nid,)).fetchone():
            out.append({"id": nid, "kind": nid.split(":")[0], "time": t, "text": text})
    return out


def in_window(local: datetime) -> bool:
    return FROM_HOUR <= local.hour < TO_HOUR


def run(
    db: sqlite3.Connection,
    items: list[dict],
    tyres: dict | None,
    now: int,
    local: datetime,
    publish: Callable[[dict], None],
) -> list[dict]:
    """Send the pending notices when inside the day window; returns those sent.

    When the tyre hint is gone, its sent rows are dropped: a hint that comes
    back counts as new."""
    if tyres is not None and not tyres["hint"]:
        db.execute("DELETE FROM notices WHERE id LIKE 'tyre_hint:%'")
        db.commit()
    if not in_window(local):
        return []
    return [n for n in pending(db, items, tyres, now, local) if plugcheck.send(db, n, publish, now)]
