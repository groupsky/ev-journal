"""Tyre sets: which set is on, km per set, age from the DOT code, and a
season-swap hint from the outside temperature (#45). See docs/design.md, "Tyres".

The sets live in the same SQLite file as places, with the same sync rules.
"""

import re
import sqlite3
from dataclasses import dataclass
from datetime import date

SCHEMA = """
CREATE TABLE IF NOT EXISTS tyre_sets (id TEXT PRIMARY KEY, name TEXT NOT NULL,
  season TEXT NOT NULL, size TEXT, dot TEXT, retired INTEGER NOT NULL DEFAULT 0,
  note TEXT, version INTEGER NOT NULL, updated_at INTEGER NOT NULL,
  deleted INTEGER NOT NULL DEFAULT 0, seq INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS tyre_swaps (id INTEGER PRIMARY KEY AUTOINCREMENT,
  date TEXT NOT NULL, km REAL NOT NULL, set_id TEXT NOT NULL,
  created_at INTEGER NOT NULL);
"""

DOT = re.compile(r"([0-9]{2})([0-9]{2})")


@dataclass(frozen=True)
class Rules:
    max_age_years: float = 6.0
    swap_c: float = 7.0  # the season line, °C
    swap_days: int = 7  # days with a reading that the mean is taken over


def init(db: sqlite3.Connection) -> None:
    db.executescript(SCHEMA)


def dot_date(dot: str) -> date:
    """The Monday of the week in a DOT code WWYY ("0721": week 7 of 2021).
    ValueError for anything else."""
    m = DOT.fullmatch(dot)
    week = int(m.group(1)) if m else 0
    if not 1 <= week <= 53:
        raise ValueError("a DOT code is 4 digits WWYY, week 01 to 53")
    return date.fromisocalendar(2000 + int(m.group(2)), week, 1)


def age_years(dot: str | None, today: date) -> float | None:
    """Not rounded; round only to show it."""
    if not dot:
        return None
    return (today - dot_date(dot)).days / 365.25


def km_per_set(swaps: list[dict], km_now: float | None) -> dict[str, float]:
    """The km each set did: from each swap to the next; the set on now runs
    to km_now (to its swap km when there is no reading). swaps oldest first."""
    out: dict[str, float] = {}
    for i, s in enumerate(swaps):
        if i + 1 < len(swaps):
            end = swaps[i + 1]["km"]
        else:
            end = km_now if km_now is not None else s["km"]
        out[s["set_id"]] = out.get(s["set_id"], 0.0) + max(0.0, end - s["km"])
    return {k: round(v, 1) for k, v in out.items()}


def hint(fitted: dict | None, sets: list[dict], temps: list[float], rules: Rules) -> str | None:
    """ "winter" or "summer" when it is time to swap, else None. temps: one
    mean per day with a reading, oldest first."""
    if not fitted or fitted["season"] == "all" or len(temps) < rules.swap_days:
        return None
    other = "winter" if fitted["season"] == "summer" else "summer"
    if not any(s["season"] == other and not s["retired"] for s in sets):
        return None
    days = temps[-rules.swap_days :]
    mean = sum(days) / len(days)
    if other == "winter" and mean < rules.swap_c:
        return "winter"
    if other == "summer" and mean > rules.swap_c:
        return "summer"
    return None


def overview(
    sets: list[dict],
    swaps: list[dict],
    today: date,
    km_now: float | None,
    temps: list[float],
    rules: Rules,
) -> dict:
    """What GET /tyres answers. sets: live rows; swaps: oldest first."""
    km = km_per_set(swaps, km_now)
    last = swaps[-1] if swaps else None
    out = []
    for s in sets:
        age = age_years(s["dot"], today)
        out.append(
            {
                **s,
                "retired": bool(s["retired"]),
                "km": km.get(s["id"], 0.0),
                "age_years": round(age, 1) if age is not None else None,
                "age_warning": age is not None and age > rules.max_age_years,
                "fitted_since": last["date"] if last and last["set_id"] == s["id"] else None,
            }
        )
    fitted = next((s for s in out if last and s["id"] == last["set_id"]), None)
    return {
        "km_now": km_now,
        "fitted": fitted,
        "hint": hint(fitted, out, temps, rules),
        "age_warning": [s["name"] for s in out if s["age_warning"] and not s["retired"]],
        "sets": out,
    }


def load(db: sqlite3.Connection) -> tuple[list[dict], list[dict]]:
    """The live sets (by name) and all swaps, oldest first."""
    sets = [dict(r) for r in db.execute("SELECT * FROM tyre_sets WHERE deleted = 0 ORDER BY name")]
    swaps = [dict(r) for r in db.execute("SELECT * FROM tyre_swaps ORDER BY date, km, id")]
    return sets, swaps


def mqtt_part(o: dict) -> dict:
    """For the retained summary: the fitted set, the hint and the old sets."""
    f = o["fitted"]
    return {
        "fitted": {k: f[k] for k in ("name", "season", "age_years")} if f else None,
        "hint": o["hint"],
        "age_warning": o["age_warning"],
    }
