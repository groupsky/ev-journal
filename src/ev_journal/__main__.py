"""ev-journal [loop|run|dry-run]

loop (the default): run every EVJ_INTERVAL_SEC seconds.
run: read, recompute everything, write to InfluxDB and MQTT once.
dry-run: read and recompute, print counts per month, write nothing.
"""

import sys
import time
from zoneinfo import ZoneInfo

from ev_journal import cleanup, outputs, places, source
from ev_journal.influx import Influx
from ev_journal.journeys import Result, build
from ev_journal.settings import Settings


def compute(s: Settings) -> Result:
    store = places.open_store(s.data_dir)
    if s.places_file:
        places.load_seed(store, s.places_file)
    pl = places.all_places(store)
    store.close()
    db = Influx(s.influx_url, s.influx_db, s.influx_read_user, s.influx_read_password)
    trips, charges = source.trips(db), source.charges(db)
    if not trips:
        return build([], [], [], pl, ZoneInfo(s.tz))
    start = min(t.start for t in trips) - 86_400_000
    end = int(time.time() * 1000) + 60_000
    return build(
        trips,
        charges,
        source.fixes(db, start, end),
        pl,
        ZoneInfo(s.tz),
        odometer=source.odometer(db, start, end),
    )


def run(s: Settings) -> None:
    r = compute(s)
    db = Influx(s.influx_url, s.influx_db, s.influx_write_user, s.influx_write_password)
    lines = outputs.influx_lines(r)
    db.write(lines)
    if s.mqtt_url:
        from ev_journal import mqtt

        mqtt.publish(s.mqtt_url, outputs.summary(r, int(time.time() * 1000), ZoneInfo(s.tz)))
    reader = Influx(s.influx_url, s.influx_db, s.influx_read_user, s.influx_read_password)
    deleted = cleanup.cleanup(reader, db, r)
    print(
        f"ev-journal: {len(r.trips)} trips, {len(r.journeys)} journeys, {len(lines)} rows, "
        f"{deleted} stale deleted"
    )


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


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "loop"
    s = Settings.from_env()
    if cmd == "dry-run":
        dry_run(s)
    elif cmd == "run":
        run(s)
    elif cmd == "loop":
        while True:
            try:
                run(s)
            except Exception as e:  # keep the loop alive; the next run recomputes all
                print(f"ev-journal: run failed: {e!r}", file=sys.stderr)
            time.sleep(s.interval_sec)
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
