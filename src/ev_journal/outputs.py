"""Rows for InfluxDB and the MQTT summary."""

import sys
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from ev_journal.geo import thin
from ev_journal.influx import line
from ev_journal.journeys import Journey, Month, Result
from ev_journal.overview import overview
from ev_journal.places import Place

TRACK_STEP_M = 50.0


def _pid(p):
    return p.id if p else None


def influx_lines(r: Result) -> list[str]:
    out = []
    for t in r.trips:
        out.append(
            line(
                "ev_trips",
                {},
                {
                    "end_ts": t.end,
                    "km": t.km,
                    "km_source": t.km_source,
                    "kwh": t.kwh,
                    "waiting_sec": t.waiting_sec,
                    "waiting_kwh": t.waiting_kwh,
                    "start_place": _pid(t.start_place),
                    "end_place": _pid(t.end_place),
                    "estimated": t.estimated,
                    "cost_eur": t.cost_eur,
                    "price_eur_per_kwh": t.price_eur_per_kwh,
                    "price_estimated": t.price_estimated,
                    "unknown_share": t.unknown_share,
                },
                t.start,
            )
        )
    for j in r.journeys:
        out.append(
            line(
                "ev_journeys",
                {},
                {
                    "end_ts": j.end,
                    "km": j.km,
                    "drive_min": j.drive_min,
                    "waiting_min": j.waiting_min,
                    "charge_min": j.charge_min,
                    "charges": len(j.charges),
                    "kwh_used": j.kwh_used,
                    "kwh_charged": j.kwh_charged,
                    "trips": len(j.trips),
                    "cost_eur": j.cost_eur,
                    "price_estimated": j.price_estimated,
                    "label": j.label,
                    "label_by_hand": j.label_by_hand,
                    "not_me": j.not_me,
                    "start_place": _pid(j.start_place),
                    "end_place": _pid(j.end_place),
                },
                j.start,
            )
        )
        for f in thin([p for t in j.trips for p in t.fixes], TRACK_STEP_M):
            out.append(line("ev_track", {"journey": str(j.id)}, {"lat": f[1], "lon": f[2]}, f[0]))
    for o in r.outings:
        out.append(
            line(
                "ev_outings",
                {},
                {"end_ts": o.end, "km": o.km, "kwh": o.kwh, "journeys": len(o.journeys)},
                o.start,
            )
        )
    for f in r.forecasts:
        out.append(
            line(
                "ev_forecast",
                {},
                {
                    "day_type": f.day_type,
                    "normal_kwh": f.normal_kwh,
                    "bad_kwh": f.bad_kwh,
                    "real_kwh": f.real_kwh,
                },
                f.start,
            )
        )
    for m in r.months:
        out.append(
            line(
                "ev_month",
                {},
                {
                    "month": m.key,
                    "trips": m.trips,
                    "km": m.km,
                    "kwh_used": m.kwh_used,
                    "kwh_home": m.kwh_home,
                    "kwh_away": m.kwh_away,
                    "kwh_unknown": m.kwh_unknown,
                    "journeys": m.journeys,
                    "outings": m.outings,
                    "commute_km": m.commute_km,
                    "spent_eur": m.spent_eur,
                    "driving_cost_eur": m.driving_cost_eur,
                    "drain_kwh": m.drain_kwh,
                    "eur_per_100km": m.eur_per_100km,
                    "kwh_per_100km": m.kwh_per_100km,
                    "eur_per_kwh": m.eur_per_kwh(),
                    "eur_per_kwh_home": m.eur_per_kwh("home"),
                    "eur_per_kwh_away": m.eur_per_kwh("away"),
                    "odo_km": m.odo_km,
                    "gap_pct": m.gap_pct,
                    "kwh_per_pct": m.kwh_per_pct,
                    "meter_ratio": m.meter_ratio,
                    "overhead_kwh": m.overhead_kwh,
                    "drift_flag": m.drift_flag,
                },
                m.start,
            )
        )
    return out


def _r(v: float | None, n: int) -> float | None:
    return None if v is None else round(v, n)


def _iso(t: int) -> str:
    return datetime.fromtimestamp(t / 1000, UTC).isoformat().replace("+00:00", "Z")


def journey_item(j: Journey) -> dict:
    """One journey as in the MQTT summary and GET /journeys."""
    return {
        "id": j.id,
        "start": _iso(j.start),
        "end": _iso(j.end),
        "from": j.start_place.name if j.start_place else None,
        "to": j.end_place.name if j.end_place else None,
        "from_place_id": j.start_place.id if j.start_place else None,
        "to_place_id": j.end_place.id if j.end_place else None,
        "label": j.label,
        "not_me": j.not_me,
        "km": round(j.km, 1),
        "kwh_used": round(j.kwh_used, 2),
        "drive_min": round(j.drive_min),
        "waiting_min": round(j.waiting_min),
        "charge_min": round(j.charge_min),
        "charges": len(j.charges),
        "kwh_charged": round(j.kwh_charged, 2),
        "cost_eur": _r(j.cost_eur, 2),
    }


def summary(r: Result, now: int, tz: ZoneInfo) -> dict:
    """The retained MQTT summary: the last 10 journeys and the current month."""
    last = [journey_item(j) for j in reversed(r.journeys[-10:])]
    key = datetime.fromtimestamp(now / 1000, tz).strftime("%Y-%m")
    m = next((m for m in r.months if m.key == key), None) or Month(start=0, key=key)
    month = {
        "month": m.key,
        "km": round(m.km, 1),
        "kwh_used": round(m.kwh_used, 2),
        "kwh_home": round(m.kwh_home, 2),
        "kwh_away": round(m.kwh_away, 2),
        "kwh_unknown": round(m.kwh_unknown, 2),
        "journeys": m.journeys,
        "outings": m.outings,
        "commute_km": round(m.commute_km, 1),
        "spent_eur": round(m.spent_eur, 2),
        "driving_cost_eur": round(m.driving_cost_eur, 2),
        "eur_per_100km": _r(m.eur_per_100km, 2),
        "kwh_per_100km": _r(m.kwh_per_100km, 1),
        "eur_per_kwh": _r(m.eur_per_kwh(), 3),
        "eur_per_kwh_home": _r(m.eur_per_kwh("home"), 3),
        "eur_per_kwh_away": _r(m.eur_per_kwh("away"), 3),
        "drift_flag": m.drift_flag,
    }
    return {
        "updated": _iso(now),
        "journeys": last,
        "month": month,
        "overview": {p: _rounded(v) for p, v in overview(r, now).items()},
        "home_charge": _home_charge(r.home_charge),
        "advice": r.advice,
    }


def _rounded(st: dict) -> dict:
    """Prices to 3 decimals, per 100 km to 2 and 1; counts stay as they are."""
    n = {"eur_per_100km": 2, "kwh_per_100km": 1}
    return {k: v if isinstance(v, int) else _r(v, n.get(k, 3)) for k, v in st.items()}


def _home_charge(h: dict | None) -> dict | None:
    if h is None:
        return None
    return {
        "running": h["running"],
        "since": _iso(h["since"]),
        "soc_plugin": h["soc_plugin"],
        "soc_now": round(h["soc_now"], 1),
        "meter_kwh": round(h["meter_kwh"], 2),
        "target": h["target"],
        "meter_kwh_to_target": round(h["meter_kwh_to_target"], 2),
        "min_to_target": _r(h["min_to_target"], 0),
    }


def home_message(places: list[Place], now: int) -> dict | None:
    """The retained home place for homy (#50). None when there is no home place.
    With several, the first by id, and a warning."""
    homes = sorted((p for p in places if p.kind == "home"), key=lambda p: p.id)
    if not homes:
        return None
    if len(homes) > 1:
        print(f"ev-journal: {len(homes)} home places, using {homes[0].id}", file=sys.stderr)
    h = homes[0]
    return {"updated": _iso(now), "id": h.id, "lat": h.lat, "lon": h.lon, "radius_m": h.radius_m}
