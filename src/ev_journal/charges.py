"""Away charges: the station, the price per battery kWh and the network kWh.

Rules: docs/charges.md. Price sources, best first: receipt, answer, estimate.
"""

import bisect
import json
import os
import re
import sqlite3
import statistics
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ev_journal.geo import dist_m
from ev_journal.journeys import Result, Track
from ev_journal.places import Place, place_at

FINES_URL = "https://public.finescharging.com/v1/locations.geojson"
FEED_MAX_AGE_MS = 24 * 3_600_000
MATCH_RADIUS_M = 75.0
FIX_WINDOW_MS = 30 * 60_000  # the position at a charge: a fix this close in time
QUESTION_AFTER_MS = 24 * 3_600_000
QUESTION_LOOKBACK_MS = 14 * 86_400_000  # no questions for older charges
RETRY_AFTER_MS = 3_600_000  # after a failed fetch, wait this long
DEFAULT_LOSS = 1.10  # network kWh per battery kWh, until receipts teach better

SCHEMA = """
CREATE TABLE IF NOT EXISTS stations (id TEXT PRIMARY KEY, network TEXT NOT NULL,
  name TEXT NOT NULL, lat REAL NOT NULL, lon REAL NOT NULL, connectors TEXT NOT NULL,
  max_power_kw REAL, price_min REAL, price_max REAL, fetched_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS charges (id INTEGER PRIMARY KEY, end_ts INTEGER NOT NULL,
  battery_kwh REAL, type TEXT NOT NULL, at_home INTEGER NOT NULL, station_id TEXT,
  match TEXT NOT NULL, network_kwh REAL, price_eur_per_kwh REAL, price_source TEXT,
  cost_eur REAL);
CREATE TABLE IF NOT EXISTS receipts (charge_id INTEGER PRIMARY KEY, amount_eur REAL NOT NULL,
  network_kwh REAL NOT NULL, time INTEGER, received_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS questions (charge_id INTEGER PRIMARY KEY,
  opened_at INTEGER NOT NULL, station_name TEXT, battery_kwh REAL,
  answered_at INTEGER, amount_eur REAL, network_kwh REAL);
"""


@dataclass(frozen=True)
class Station:
    id: str
    name: str
    lat: float
    lon: float
    price_min: float | None
    price_max: float | None


def init(db: sqlite3.Connection) -> None:
    db.executescript(SCHEMA)


# --- the feed ---------------------------------------------------------------


def _http_get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "ev-journal (private, daily)"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


def parse_fines(raw: bytes) -> list[dict]:
    d = json.loads(raw)
    if d.get("type") != "FeatureCollection" or not d.get("features"):
        raise ValueError("not a Fines locations file")
    out = []
    for f in d["features"]:
        p, (lon, lat) = f["properties"], f["geometry"]["coordinates"][:2]
        pr = p.get("price_range") or {}
        out.append(
            {
                "id": f"fines:{f['id']}",
                "name": p["name"],
                "lat": lat,
                "lon": lon,
                "connectors": json.dumps(p.get("connectors") or {}),
                "max_power_kw": p.get("max_power_kw"),
                "price_min": pr.get("min"),
                "price_max": pr.get("max"),
            }
        )
    return out


def update_stations(
    db: sqlite3.Connection,
    cache: Path,
    now: int,
    fetch: Callable[[str], bytes] = _http_get,
) -> str:
    """Fetch the Fines feed at most once a day. Keep the last good copy when it fails.

    Returns "cached", "fetched" or "failed: <reason>".
    """
    tried = cache.with_suffix(".tried")
    fresh = cache.exists() and now - cache.stat().st_mtime * 1000 < FEED_MAX_AGE_MS
    waiting = tried.exists() and now - tried.stat().st_mtime * 1000 < RETRY_AFTER_MS
    if fresh or (waiting and cache.exists()):
        status = "cached"
        raw = cache.read_bytes()
    elif waiting:
        return "failed: waiting before the next try"
    else:
        tried.touch()
        try:
            raw = fetch(FINES_URL)
            parse_fines(raw)  # check before we keep it
            tmp = cache.with_suffix(".tmp")
            tmp.write_bytes(raw)
            os.replace(tmp, cache)
            status = "fetched"
        except Exception as e:  # keep the last good copy
            if not cache.exists():
                return f"failed: {e!r}"
            raw, status = cache.read_bytes(), f"failed: {e!r}"
    rows = parse_fines(raw)
    with db:
        db.execute("DELETE FROM stations WHERE network = 'fines'")
        db.executemany(
            "INSERT INTO stations VALUES (:id, 'fines', :name, :lat, :lon, :connectors,"
            " :max_power_kw, :price_min, :price_max, :fetched_at)",
            [{**r, "fetched_at": int(cache.stat().st_mtime * 1000)} for r in rows],
        )
    return status


def all_stations(db: sqlite3.Connection) -> list[Station]:
    return [
        Station(*r)
        for r in db.execute("SELECT id, name, lat, lon, price_min, price_max FROM stations")
    ]


# --- the match --------------------------------------------------------------


def position(track: Track, start: int, end: int) -> tuple[float, float] | None:
    """The real fix nearest in time: the last before the start or the first after the end."""
    best = None
    i = bisect.bisect_right(track.times, start)
    if i:
        best = (start - track.times[i - 1], track.fixes[i - 1])
    k = bisect.bisect_left(track.times, end)
    if k < len(track.times) and (best is None or track.times[k] - end < best[0]):
        best = (track.times[k] - end, track.fixes[k])
    if best is None or best[0] > FIX_WINDOW_MS:
        return None
    return best[1][1], best[1][2]


def match(pos: tuple[float, float] | None, stations: list[Station]) -> tuple[str | None, str]:
    """The nearest station within 75 m. Returns (station id, "ok" | "unsure" | "no station")."""
    if pos is None:
        return None, "no position"
    near = sorted(
        (d, s.id) for s in stations if (d := dist_m(pos[0], pos[1], s.lat, s.lon)) <= MATCH_RADIUS_M
    )
    if not near:
        return None, "no station"
    return near[0][1], "ok" if len(near) == 1 else "unsure"


def sync(db: sqlite3.Connection, r: Result, places: list[Place], now: int) -> None:
    """Store every charge with its station, open the questions, and price them.

    Charges that are gone from homy are deleted, with their open questions.
    Receipts and answers stay.
    """
    stations = all_stations(db)
    ids = [c.start for c in r.charges]
    # With no charges at all (an empty read), NOT IN (NULL) deletes nothing, on purpose.
    marks = ",".join("?" * len(ids)) or "NULL"
    db.execute(f"DELETE FROM charges WHERE id NOT IN ({marks})", ids)
    db.execute(
        f"DELETE FROM questions WHERE answered_at IS NULL AND charge_id NOT IN ({marks})", ids
    )
    for c in r.charges:
        pos = position(r.track, c.start, c.end) if r.track else None
        here = place_at(places, pos) or c.place
        at_home = here is not None and here.kind == "home"
        sid, how = (None, "home") if at_home else match(pos, stations)
        db.execute(
            "INSERT INTO charges (id, end_ts, battery_kwh, type, at_home, station_id, match)"
            " VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET end_ts = excluded.end_ts,"
            " battery_kwh = excluded.battery_kwh, type = excluded.type,"
            " at_home = excluded.at_home, station_id = excluded.station_id,"
            " match = excluded.match",
            (c.start, c.end, c.kwh, c.type, int(at_home), sid, how),
        )
    # A charge that is at home now (a place was added) is not priced here.
    db.execute(
        "UPDATE charges SET network_kwh = NULL, price_eur_per_kwh = NULL, price_source = NULL,"
        " cost_eur = NULL WHERE at_home = 1"
    )
    db.execute(
        "DELETE FROM questions WHERE answered_at IS NULL"
        " AND charge_id IN (SELECT id FROM charges WHERE at_home = 1)"
    )
    open_questions(db, now)
    for (cid,) in db.execute("SELECT id FROM charges WHERE at_home = 0").fetchall():
        price(db, cid)
    db.commit()


def open_questions(db: sqlite3.Connection, now: int) -> None:
    """A question for each recent away charge with no receipt, 24 h after it ends."""
    db.execute(
        "INSERT OR IGNORE INTO questions (charge_id, opened_at, battery_kwh)"
        " SELECT c.id, ?, c.battery_kwh FROM charges c"
        " WHERE c.at_home = 0 AND c.end_ts <= ? AND c.end_ts >= ?"
        " AND c.id NOT IN (SELECT charge_id FROM receipts)",
        (now, now - QUESTION_AFTER_MS, now - QUESTION_LOOKBACK_MS),
    )


# --- the price --------------------------------------------------------------


def parse_answer(text: str) -> tuple[float, float]:
    """ "5.00 12.5" -> (amount in EUR, network kWh). A comma works as the decimal mark."""
    nums = re.findall(r"\d+(?:[.,]\d+)?", text)
    if len(nums) != 2:
        raise ValueError("expected two numbers: the amount and the network kWh")
    amount, kwh = (float(n.replace(",", ".")) for n in nums)
    if amount <= 0 or kwh <= 0:
        raise ValueError("both numbers must be above 0")
    return amount, kwh


def loss_ratio(db: sqlite3.Connection) -> float:
    """Network kWh per battery kWh: the median of the known charges, or the default."""
    # One value per charge; a receipt wins over an answer.
    rows = db.execute(
        "SELECT COALESCE(r.network_kwh, q.network_kwh) / c.battery_kwh FROM charges c"
        " LEFT JOIN receipts r ON r.charge_id = c.id"
        " LEFT JOIN questions q ON q.charge_id = c.id AND q.answered_at IS NOT NULL"
        " WHERE c.battery_kwh > 0 AND COALESCE(r.network_kwh, q.network_kwh) IS NOT NULL"
    ).fetchall()
    return statistics.median(r[0] for r in rows) if rows else DEFAULT_LOSS


def price(db: sqlite3.Connection, charge_id: int) -> None:
    """Set the price per battery kWh, its source, the network kWh and the cost."""
    c = db.execute(
        "SELECT c.battery_kwh, c.type, s.price_min, s.price_max FROM charges c"
        " LEFT JOIN stations s ON s.id = c.station_id WHERE c.id = ?",
        (charge_id,),
    ).fetchone()
    if c is None:
        return
    battery, kind, pmin, pmax = c
    rec = db.execute(
        "SELECT amount_eur, network_kwh FROM receipts WHERE charge_id = ?", (charge_id,)
    ).fetchone()
    ans = db.execute(
        "SELECT amount_eur, network_kwh FROM questions"
        " WHERE charge_id = ? AND answered_at IS NOT NULL",
        (charge_id,),
    ).fetchone()
    source, cost, network = None, None, None
    if rec:
        source, (cost, network) = "receipt", rec
    elif ans:
        source, (cost, network) = "answer", ans
    elif battery and (pmin is not None or pmax is not None):
        # The feed gives one range per site. AC is the low end, DC the high end.
        unit = pmin if kind == "AC" and pmin is not None else (pmax if pmax is not None else pmin)
        network = battery * loss_ratio(db)
        source, cost = "estimate", unit * network
    per_kwh = cost / battery if cost is not None and battery else None
    db.execute(
        "UPDATE charges SET network_kwh = ?, price_eur_per_kwh = ?, price_source = ?,"
        " cost_eur = ? WHERE id = ?",
        (network, per_kwh, source, cost, charge_id),
    )
