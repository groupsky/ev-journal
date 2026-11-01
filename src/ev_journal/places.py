"""Places: loaded from a seed file outside the repo into the SQLite store."""

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from ev_journal.geo import dist_m


@dataclass(frozen=True)
class Place:
    id: str
    name: str
    kind: str  # home, work, station, other
    lat: float
    lon: float
    radius_m: float


def open_store(data_dir: Path) -> sqlite3.Connection:
    data_dir.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(data_dir / "ev-journal.sqlite")
    db.execute(
        "CREATE TABLE IF NOT EXISTS places (id TEXT PRIMARY KEY, name TEXT NOT NULL,"
        " kind TEXT NOT NULL, lat REAL NOT NULL, lon REAL NOT NULL, radius_m REAL NOT NULL)"
    )
    return db


def load_seed(db: sqlite3.Connection, seed_file: Path) -> None:
    """Replace the places in the store with the ones in the seed file (a JSON list)."""
    rows = json.loads(seed_file.read_text())
    with db:
        db.execute("DELETE FROM places")
        db.executemany(
            "INSERT INTO places VALUES (?, ?, ?, ?, ?, ?)",
            [
                (str(r["id"]), r["name"], r["kind"], r["lat"], r["lon"], r.get("radius_m", 150))
                for r in rows
            ],
        )


def all_places(db: sqlite3.Connection) -> list[Place]:
    return [Place(*r) for r in db.execute("SELECT id, name, kind, lat, lon, radius_m FROM places")]


def place_at(places: list[Place], pos: tuple[float, float] | None) -> Place | None:
    """The nearest place whose radius holds the position."""
    if pos is None:
        return None
    best, best_d = None, None
    for p in places:
        d = dist_m(p.lat, p.lon, pos[0], pos[1])
        if d <= p.radius_m and (best_d is None or d < best_d):
            best, best_d = p, d
    return best
