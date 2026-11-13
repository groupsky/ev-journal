"""The SQLite store: places, routes, journey labels, the command log and sent notices.

Places and routes have a version and a delete marker, so an offline client
can sync with `changed_since`. One user: the last write wins.
"""

import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from ev_journal.geo import dist_m

SCHEMA = """
CREATE TABLE IF NOT EXISTS places (id TEXT PRIMARY KEY, name TEXT NOT NULL,
  kind TEXT NOT NULL, lat REAL NOT NULL, lon REAL NOT NULL, radius_m REAL NOT NULL);
CREATE TABLE IF NOT EXISTS routes (id TEXT PRIMARY KEY, name TEXT NOT NULL,
  place_ids TEXT NOT NULL, version INTEGER NOT NULL, updated_at INTEGER NOT NULL,
  deleted INTEGER NOT NULL DEFAULT 0, seq INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS labels (journey_id INTEGER PRIMARY KEY, label TEXT,
  not_me INTEGER NOT NULL DEFAULT 0, updated_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS commands (client_id TEXT PRIMARY KEY, time INTEGER NOT NULL,
  text TEXT NOT NULL, action TEXT NOT NULL, place_id TEXT, route_id TEXT,
  received_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS notices (id TEXT PRIMARY KEY, time INTEGER NOT NULL);
"""

# Columns added to the places table of #3.
PLACE_COLUMNS = {
    "aliases": "TEXT NOT NULL DEFAULT '[]'",
    "destination": "INTEGER NOT NULL DEFAULT 0",
    "version": "INTEGER NOT NULL DEFAULT 1",
    "updated_at": "INTEGER NOT NULL DEFAULT 0",
    "deleted": "INTEGER NOT NULL DEFAULT 0",
    "seq": "INTEGER NOT NULL DEFAULT 0",  # goes up on every write; the sync cursor
}


@dataclass(frozen=True)
class Place:
    id: str
    name: str
    kind: str  # home, work, station, other
    lat: float
    lon: float
    radius_m: float


def now_ms() -> int:
    return int(time.time() * 1000)


def store_path(data_dir: Path) -> Path:
    return data_dir / "ev-journal.sqlite"


def open_store(data_dir: Path) -> sqlite3.Connection:
    data_dir.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(store_path(data_dir))
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    from ev_journal import charges, tyres, upkeep  # their tables live in the same file

    charges.init(db)
    upkeep.init(db)
    tyres.init(db)
    have = {r["name"] for r in db.execute("PRAGMA table_info(places)")}
    for col, decl in PLACE_COLUMNS.items():
        if col not in have:
            db.execute(f"ALTER TABLE places ADD COLUMN {col} {decl}")
    if "seq" not in have:
        db.execute("UPDATE places SET seq = rowid")
    db.commit()
    return db


def load_seed(db: sqlite3.Connection, seed_file: Path) -> bool:
    """Import the seed file (a JSON list) once, into an empty store.

    After that, places are changed through the API, and the seed is ignored.
    """
    if db.execute("SELECT 1 FROM places LIMIT 1").fetchone():
        return False
    rows = json.loads(seed_file.read_text())
    t = now_ms()
    with db:
        db.executemany(
            "INSERT INTO places (id, name, kind, lat, lon, radius_m, aliases, destination,"
            " version, updated_at, seq) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)",
            [
                (
                    str(r["id"]),
                    r["name"],
                    r["kind"],
                    r["lat"],
                    r["lon"],
                    r.get("radius_m", 150),
                    json.dumps(r.get("aliases", [])),
                    int(r.get("destination", False)),
                    t,
                    i + 1,
                )
                for i, r in enumerate(rows)
            ],
        )
    return True


def all_places(db: sqlite3.Connection) -> list[Place]:
    return [
        Place(r["id"], r["name"], r["kind"], r["lat"], r["lon"], r["radius_m"])
        for r in db.execute(
            "SELECT id, name, kind, lat, lon, radius_m FROM places WHERE deleted = 0"
        )
    ]


def all_labels(db: sqlite3.Connection) -> list[tuple[int, str | None, bool]]:
    return [
        (r["journey_id"], r["label"], bool(r["not_me"]))
        for r in db.execute(
            "SELECT journey_id, label, not_me FROM labels ORDER BY updated_at, journey_id"
        )
    ]


def place_at(places: list[Place], pos: tuple[float, float] | None) -> Place | None:
    """The nearest place whose radius holds the position.

    A home place that holds it wins, even when another place is nearer (#28).
    """
    if pos is None:
        return None
    best, best_key = None, None
    for p in places:
        d = dist_m(p.lat, p.lon, pos[0], pos[1])
        key = (p.kind != "home", d)
        if d <= p.radius_m and (best_key is None or key < best_key):
            best, best_key = p, key
    return best
