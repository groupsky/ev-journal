"""ev-journal [loop|run|dry-run]

loop (the default): run every EVJ_INTERVAL_SEC seconds, and serve the API
  on EVJ_API_PORT when EVJ_API_TOKEN_FILE is set.
run: read, recompute everything, write to InfluxDB and MQTT once.
dry-run: read and recompute, print counts per month, write nothing.
"""

import sys
import threading
import time
from zoneinfo import ZoneInfo

from ev_journal import charges, cleanup, outputs, places, source
from ev_journal.influx import Influx
from ev_journal.journeys import Result, build
from ev_journal.settings import Settings


def compute(s: Settings) -> Result:
    store = places.open_store(s.data_dir)
    if s.places_file:
        places.load_seed(store, s.places_file)
    pl = places.all_places(store)
    labels = places.all_labels(store)
    store.close()
    db = Influx(s.influx_url, s.influx_db, s.influx_read_user, s.influx_read_password)
    trips, charges = source.trips(db), source.charges(db)
    if not trips:
        return build([], [], [], pl, ZoneInfo(s.tz), labels=labels)
    start = min(t.start for t in trips) - 86_400_000
    end = int(time.time() * 1000) + 60_000
    return build(
        trips,
        charges,
        source.fixes(db, start, end),
        pl,
        ZoneInfo(s.tz),
        odometer=source.odometer(db, start, end),
        labels=labels,
    )


def away(s: Settings, r: Result) -> str:
    """Update the station feed (daily) and the away charges in the store."""
    db = places.open_store(s.data_dir)
    try:
        now = int(time.time() * 1000)
        status = charges.update_stations(db, s.data_dir / "fines-locations.geojson", now)
        charges.sync(db, r, places.all_places(db), now)
        return status
    finally:
        db.close()


def run(s: Settings) -> Result:
    r = compute(s)
    db = Influx(s.influx_url, s.influx_db, s.influx_write_user, s.influx_write_password)
    lines = outputs.influx_lines(r)
    db.write(lines)
    if s.mqtt_url:
        from ev_journal import mqtt

        mqtt.publish(s.mqtt_url, outputs.summary(r, int(time.time() * 1000), ZoneInfo(s.tz)))
    reader = Influx(s.influx_url, s.influx_db, s.influx_read_user, s.influx_read_password)
    deleted = cleanup.cleanup(reader, db, r)
    try:
        feed = away(s, r)
    except Exception as e:  # the main outputs are written already
        feed = f"failed: {e!r}"
    print(
        f"ev-journal: {len(r.trips)} trips, {len(r.journeys)} journeys, {len(lines)} rows, "
        f"{deleted} stale deleted, station feed {feed}"
    )
    return r


def journey_ends(r: Result) -> list[tuple[int, float, float]]:
    return [(j.end, *j.trips[-1].end_pos) for j in r.journeys if j.trips[-1].end_pos]


def serve_api(s: Settings, state: dict) -> None:
    import uvicorn

    from ev_journal.api import create_app

    places.open_store(s.data_dir).close()  # create or upgrade the tables
    app = create_app(
        places.store_path(s.data_dir),
        s.api_token,
        ZoneInfo(s.tz),
        lambda: state["stops"],
        lambda: state["journeys"],
    )
    server = uvicorn.Server(uvicorn.Config(app, host=s.api_host, port=s.api_port))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            return
        if not thread.is_alive():
            break
        time.sleep(0.1)
    sys.exit(f"ev-journal: the API did not start on {s.api_host}:{s.api_port}")


def dry_run(s: Settings) -> None:
    r = compute(s)
    print("month    trips journeys outings  gps_km  odo_km   gap%  gps_trips")
    by_month = {}
    for t in r.trips:
        key = next(m.key for m in reversed(r.months) if m.start <= t.start)
        by_month.setdefault(key, []).append(t)
    for m in r.months:
        gps = sum(t.km_source == "gps" for t in by_month.get(m.key, []))
        odo = f"{m.odo_km:7.0f}" if m.odo_km is not None else "      -"
        gap = f"{m.gap_pct:+6.2f}" if m.gap_pct is not None else "     -"
        print(
            f"{m.key}  {m.trips:5d} {m.journeys:8d} {m.outings:7d} {m.km:7.0f} {odo} {gap}"
            f"  {gps}/{m.trips}"
        )
    labels = {}
    for j in r.journeys:
        labels[j.label] = labels.get(j.label, 0) + 1
    placed = sum(j.start_place is not None and j.end_place is not None for j in r.journeys)
    print(f"labels: {labels}; journeys with both places known: {placed}/{len(r.journeys)}")
    # The store is local to this run (EVJ_DATA_DIR); nothing goes to InfluxDB or MQTT.
    print(f"station feed: {away(s, r)}")
    db = places.open_store(s.data_dir)
    rows = db.execute("SELECT id, match FROM charges WHERE at_home = 0 ORDER BY id").fetchall()
    first = next((cid for cid, how in rows if how in ("ok", "unsure")), None)
    since = [how for cid, how in rows if first is not None and cid >= first]
    counts = {k: since.count(k) for k in sorted(set(since))}
    share = (counts.get("ok", 0) + counts.get("unsure", 0)) / len(since) * 100 if since else 0
    print(
        f"away charges: {len(rows)}; since the first matched one: {len(since)}, {counts},"
        f" matched {share:.0f} %"
    )
    db.close()


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "loop"
    s = Settings.from_env()
    if cmd == "dry-run":
        dry_run(s)
    elif cmd == "run":
        run(s)
    elif cmd == "loop":
        state = {"stops": [], "journeys": []}
        if s.api_token:
            serve_api(s, state)
        while True:
            try:
                r = run(s)
                state["stops"] = journey_ends(r)
                state["journeys"] = [(j.start, j.end) for j in r.journeys]
            except Exception as e:  # keep the loop alive; the next run recomputes all
                print(f"ev-journal: run failed: {e!r}", file=sys.stderr)
            time.sleep(s.interval_sec)
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
