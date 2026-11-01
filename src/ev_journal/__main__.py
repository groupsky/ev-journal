"""ev-journal [loop|run|dry-run]

loop (the default): run every EVJ_INTERVAL_SEC seconds, and serve the API
  on EVJ_API_PORT when EVJ_API_TOKEN_FILE is set.
run: read, recompute everything, write to InfluxDB and MQTT once.
dry-run: read and recompute, print counts per month, write nothing.
"""

import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path
from zoneinfo import ZoneInfo

from ev_journal import charges, cleanup, outputs, places, price, source
from ev_journal.influx import Influx
from ev_journal.journeys import Result, build
from ev_journal.settings import Settings


def compute(s: Settings) -> Result:
    store = places.open_store(s.data_dir)
    if s.places_file:
        places.load_seed(store, s.places_file)
    pl = places.all_places(store)
    labels = places.all_labels(store)
    away_costs = charges.known_costs(store)
    store.close()
    db = Influx(s.influx_url, s.influx_db, s.influx_read_user, s.influx_read_password)
    trips, sessions = source.trips(db), source.charges(db)
    if not trips:
        return build([], [], [], pl, ZoneInfo(s.tz), labels=labels)
    start = min(t.start for t in trips) - 86_400_000
    end = int(time.time() * 1000) + 60_000
    r = build(
        trips,
        sessions,
        source.fixes(db, start, end),
        pl,
        ZoneInfo(s.tz),
        odometer=source.odometer(db, start, end),
        labels=labels,
    )
    home = source.home_costs(db)
    price.apply(r, {**away_costs, **home}, set(home), ZoneInfo(s.tz))
    return r


def away(s: Settings, r: Result, data_dir=None, fetch=None) -> str:
    """Update the station feed (daily) and the away charges in the store."""
    data_dir = data_dir or s.data_dir
    db = places.open_store(data_dir)
    try:
        now = int(time.time() * 1000)
        cache = data_dir / "fines-locations.geojson"
        args = (fetch,) if fetch else ()
        status = charges.update_stations(db, cache, now, *args)
        charges.sync(db, r, places.all_places(db), now)
        return status
    finally:
        db.close()


def _no_fetch(url: str) -> bytes:
    raise RuntimeError("dry-run: cache only")


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


def by_key(t: int, r: Result) -> str:
    return next(m.key for m in reversed(r.months) if m.start <= t)


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
    # Money stays out of this print: only shares and percentages.
    print("month   priced trips  kWh/100km  home vs other charges  driving cost vs home price")
    for m in r.months:
        mc = [c for c in r.charges if by_key(c.start, r) == m.key]
        tr = [t for t in r.trips if by_key(t.start, r) == m.key]
        home = [c for c in mc if c.home_cost and c.kwh]
        others = len(mc) - len(home)
        check = "-"
        if home and m.priced_kwh:
            avg = sum(c.cost_eur for c in home) / sum(c.kwh for c in home)
            gap = (m.driving_cost_eur - m.priced_kwh * avg) / (m.priced_kwh * avg) * 100
            check = f"{gap:+.1f} %"
        k100 = f"{m.kwh_per_100km:9.1f}" if m.kwh_per_100km is not None else "        -"
        priced = sum(t.cost_eur is not None for t in tr)
        print(
            f"{m.key}  {priced:5d}/{len(tr):<5d} {k100}  {len(home):5d} / {others:<5d}"
            f"           {check}"
        )
    labels = {}
    for j in r.journeys:
        labels[j.label] = labels.get(j.label, 0) + 1
    placed = sum(j.start_place is not None and j.end_place is not None for j in r.journeys)
    print(f"labels: {labels}; journeys with both places known: {placed}/{len(r.journeys)}")
    # Work on a copy of the store, from the feed cache only: nothing is written
    # anywhere, and the Fines API is not called.
    tmp = Path(tempfile.mkdtemp(prefix="evj-dry-"))
    for name in ("ev-journal.sqlite", "fines-locations.geojson"):
        if (s.data_dir / name).exists():
            shutil.copy2(s.data_dir / name, tmp / name)
    try:
        print(f"station feed: {away(s, r, tmp, _no_fetch)}")
        db = places.open_store(tmp)
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
    finally:
        shutil.rmtree(tmp)


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
