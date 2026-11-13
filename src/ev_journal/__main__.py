"""ev-journal [loop|run|dry-run]

loop (the default): run every EVJ_INTERVAL_SEC seconds, run the plug check
  at EVJ_PLUG_CHECK_AT, and serve the API on EVJ_API_PORT when
  EVJ_API_TOKEN_FILE is set.
run: read, recompute everything, write to InfluxDB and MQTT once.
dry-run: read and recompute, print counts per month and what the plug check
  would decide now, write nothing.
"""

import shutil
import statistics
import sys
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from ev_journal import (
    charges,
    cleanup,
    forecast,
    homecharge,
    outputs,
    places,
    plugcheck,
    price,
    source,
    track,
)
from ev_journal.influx import Influx
from ev_journal.journeys import Result, build, month_key
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
    home_charge_model(s, db, r, set(home), end)
    advice(s, db, r)
    return r


def advice(s: Settings, db: Influx, r: Result) -> None:
    """Tomorrow's forecast and charging advice (#9)."""
    tz, now = ZoneInfo(s.tz), int(time.time() * 1000)
    y = time.gmtime().tm_year
    try:
        hols = forecast.holiday_set(s.holidays_country, s.days_off, range(y - 2, y + 2))
    except (ValueError, NotImplementedError) as e:  # a bad EVJ_HOLIDAYS or EVJ_DAYS_OFF
        print(f"ev-journal: no advice: {e!r}", file=sys.stderr)
        return
    soc = r.home_charge["soc_now"] if r.home_charge else source.last_soc(db, now)
    kpp = r.soc_model.kwh_per_pct if r.soc_model else homecharge.DEFAULT_KWH_PER_PCT
    r.advice = forecast.plan(
        r,
        tz,
        now,
        soc,
        kpp,
        hols,
        s.departure,
        s.reserve_pct,
        s.preheat,
        s.departures,
        s.day_ranges,
        s.timer_step_min,
        r.levels or forecast.LEVELS,
        forecast.display_map(source.soc_pairs(db, now - 90 * 86_400_000)),
    )


def home_charge_model(s: Settings, db: Influx, r: Result, home: set[int], now: int) -> None:
    """Back-test, drift per month and the live estimate of a home charge (#8)."""
    tz = ZoneInfo(s.tz)
    hc = []
    for c in r.charges:
        at_home = c.start in home or (c.place is not None and c.place.kind == "home")
        if not at_home or not c.kwh or c.soc_start is None or c.soc_end is None:
            continue
        bs = homecharge.blocks(source.meter_minutes(db, c.start - 5 * 60_000, c.end))
        bs = [b for b in bs if b.end >= c.start]
        meter, _pre = homecharge.split(bs)
        if meter > 0:
            hc.append(
                homecharge.HomeCharge(
                    c.start,
                    meter,
                    c.kwh,
                    c.soc_start,
                    c.soc_end,
                    month_key(c.start, tz)[0],
                    homecharge.charging_hours(bs),
                )
            )
    r.backtest = homecharge.backtest(hc)
    r.soc_model = homecharge.fit(hc)
    r.levels = forecast.levels(hc)
    for c in hc:
        r.level_months.setdefault(c.month, []).append(c)
    r.level_months = {k: forecast.level_rates(v) for k, v in sorted(r.level_months.items())}
    months = homecharge.drift(hc, s.drift_pct, s.drift_kwh)
    for m in r.months:
        if mm := months.get(m.key):
            m.kwh_per_pct, m.meter_ratio = mm.kwh_per_pct, mm.meter_ratio
            m.overhead_kwh, m.drift_flag = mm.overhead_kwh, mm.drift_flag
    last = r.trips[-1] if r.trips else None
    if last is None or last.end_place is None or last.end_place.kind != "home":
        return
    bs = [
        b
        for b in homecharge.blocks(source.meter_minutes(db, last.end - 5 * 60_000, now))
        if b.end >= last.end
    ]
    soc = source.last_soc(db, bs[0].start) if bs else None
    real_now = int(time.time() * 1000)
    r.home_charge = homecharge.live(bs, soc, r.soc_model, real_now, s.soc_target)


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
        publish_home(s)
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


def publish_home(s: Settings) -> None:
    """The home place, retained, for homy's car checks (#50)."""
    from ev_journal import mqtt

    store = places.open_store(s.data_dir)
    try:
        msg = outputs.home_message(places.all_places(store), int(time.time() * 1000))
    finally:
        store.close()
    if msg is not None:
        mqtt.publish(s.mqtt_url, msg, mqtt.HOME_TOPIC)


def plug_decision(s: Settings, r: Result, now: int) -> tuple[str | None, str]:
    """The plug check on fresh data: (notice text or None, the reason)."""
    db = Influx(s.influx_url, s.influx_db, s.influx_read_user, s.influx_read_password)
    last = r.trips[-1] if r.trips else None
    at_home = (
        last is not None
        and last.end_place is not None
        and last.end_place.kind == "home"
        and not source.moved_after(db, last.end)
    )
    state = "unknown"
    if at_home:
        meter = [w for _t, w, _e in source.meter_minutes(db, last.end, now)]
        state = plugcheck.plug_state(source.plug_frames(db, last.end), meter)
    return plugcheck.decide(r.advice, at_home, state)


def plug_check(s: Settings, at: int) -> None:
    """Read fresh data, decide, and publish the notice once per evening (#43).
    at: the planned check time; it gives the notice id and time."""
    r = compute(s)
    now = int(time.time() * 1000)
    text, why = plug_decision(s, r, now)
    print(f"ev-journal: plug check: {why}")
    if text is None or not s.mqtt_url:
        return
    from ev_journal import mqtt

    local = datetime.fromtimestamp(at / 1000, ZoneInfo(s.tz))
    n = plugcheck.notice(local.date().isoformat(), local.isoformat(timespec="seconds"), text)
    store = places.open_store(s.data_dir)
    try:
        plugcheck.send(
            store, n, lambda m: mqtt.publish(s.mqtt_url, m, mqtt.NOTICE_TOPIC, retain=False), now
        )
    finally:
        store.close()


def journey_ends(r: Result) -> list[tuple[int, float, float]]:
    return [(j.end, *j.trips[-1].end_pos) for j in r.journeys if j.trips[-1].end_pos]


def journey_fixes(r: Result) -> dict[int, tuple[int, int, list]]:
    """Per journey id: its start, end and real GPS fixes."""
    return {j.id: (j.start, j.end, [f for t in j.trips for f in t.fixes]) for j in r.journeys}


def journey_track(s: Settings, fixes: dict, journey_id: int) -> list[dict] | None:
    """The map points of one journey, with the energy counters read now."""
    if journey_id not in fixes:
        return None
    start, end, fx = fixes[journey_id]
    if not fx:
        return []
    db = Influx(s.influx_url, s.influx_db, s.influx_read_user, s.influx_read_password)
    return track.journey_track(fx, source.counters(db, start, end))


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
        lambda: state["advice"],
        lambda: state["advice_ctx"],
        lambda jid: journey_track(s, state["fixes"], jid),
        lambda: state["journey_items"],
        (lambda: publish_home(s)) if s.mqtt_url else lambda: None,
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
    e = r.backtest
    if e:
        print(
            f"home charge SoC back-test: {len(e)} charges, median error"
            f" {homecharge.quantile(e, 0.5):.2f} %, p90 {homecharge.quantile(e, 0.9):.2f} %"
        )
    for m in r.months:
        if m.meter_ratio is not None:
            k = f"{m.kwh_per_pct:.3f}" if m.kwh_per_pct is not None else "-"
            print(
                f"{m.key}  kWh per % {k}  meter ratio {m.meter_ratio:.3f}"
                f"  overhead {m.overhead_kwh:.2f} kWh  drift: {m.drift_flag or '-'}"
            )
    print("forecast back-test (last 91 days): days, bad day covers, normal day error")
    for dtype, sc in forecast.score(r.forecasts).items():
        print(
            f"  {dtype:8s} {sc['days']:3d}  {sc['covered_pct']:5.1f} %"
            f"  {sc['normal_error_kwh']:.1f} kWh"
        )
    print(
        "forecast choices tried (same 91 days): kind, keys, days, bad day covers, normal day error"
    )
    for t in r.forecast_tries:
        keys = "+".join(t["choice"]) or "no key"
        mark = "  <- picked" if t["picked"] else ""
        print(
            f"  {t['kind']:7s} {keys:28s} {t['days']:3d}  {t['covered_pct']:5.1f} %"
            f"  {t['normal_error_kwh']:.1f} kWh{mark}"
        )
    fe = r.forecast_effects
    if fe.get("rows"):
        print(
            f"forecast effects (walk forward, last {forecast.EFFECT_DAYS} days):"
            " keys, days, bad day covers, normal day error"
        )
        for t in fe["rows"]:
            keys = "+".join(t["keys"]) or "today's rule"
            mark = "  <- used" if t["keys"] == fe["keys"] else ""
            print(
                f"  {keys:40s} {t['days']:3d}  {t['covered_pct']:5.1f} %"
                f"  {t['normal_error_kwh']:.2f} kWh{mark}"
            )
        for t in fe["table"]:
            if t["key"] in fe["keys"] and t["effect_kwh"]:
                print(
                    f"  {t['key']} = {t['value']} ({t['kind']}):"
                    f" {t['effect_kwh']:+.1f} kWh (n = {t['n']})"
                )
    print(
        "battery kW per level: "
        + ", ".join(f"{name} {kw:.2f}" for name, _m, kw in r.levels or forecast.LEVELS)
    )
    for key, by in r.level_months.items():  # to see a season or temperature effect
        print(
            f"  {key}  "
            + ", ".join(
                f"{name} {statistics.median(by[name]):.2f} (n = {len(by[name])})"
                for name, _m, _kw in forecast.LEVELS
                if name in by
            )
        )
    if r.advice:
        print(
            f"advice: {r.advice['day_type']}, target {r.advice['target']} %,"
            f" level {r.advice['level']}, capped {r.advice['capped']}"
        )
    if s.plug_check_at:
        text, why = plug_decision(s, r, int(time.time() * 1000))
        print(f"plug check at {s.plug_check_at}: {why}; notice: {'yes' if text else 'no'}")
    else:
        print("plug check: off")
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
        state = {
            "stops": [],
            "journeys": [],
            "advice": None,
            "advice_ctx": None,
            "fixes": {},
            "journey_items": [],
        }
        if s.api_token:
            serve_api(s, state)
        tz, next_run = ZoneInfo(s.tz), 0
        now = int(time.time() * 1000)
        next_check = plugcheck.next_at(now, s.plug_check_at, tz) if s.plug_check_at else None
        while True:
            now = int(time.time() * 1000)
            if next_check is not None and now >= next_check:
                try:
                    plug_check(s, next_check)
                except Exception as e:  # one try per evening
                    print(f"ev-journal: plug check failed: {e!r}", file=sys.stderr)
                next_check = plugcheck.next_at(now, s.plug_check_at, tz)
                continue
            if now < next_run:
                time.sleep((min(next_run, next_check or next_run) - now) / 1000)
                continue
            next_run = now + s.interval_sec * 1000
            try:
                r = run(s)
                state["stops"] = journey_ends(r)
                state["journeys"] = [(j.start, j.end) for j in r.journeys]
                state["advice"] = r.advice
                state["advice_ctx"] = r.advice_ctx
                state["fixes"] = journey_fixes(r)
                state["journey_items"] = [outputs.journey_item(j) for j in r.journeys]
            except Exception as e:  # keep the loop alive; the next run recomputes all
                print(f"ev-journal: run failed: {e!r}", file=sys.stderr)
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
