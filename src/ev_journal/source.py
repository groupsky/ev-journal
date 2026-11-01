"""Read the inputs from InfluxDB (read user, SELECT only)."""

from ev_journal.influx import Influx
from ev_journal.journeys import Charge, Trip

DAY = 86_400_000
GPS_WINDOW = 7 * DAY


def _f(v) -> float | None:
    return None if v is None else float(v)


def trips(db: Influx) -> list[Trip]:
    rows = db.query("SELECT * FROM ioniq_sessions WHERE kind = 'trip'")
    return [
        Trip(
            start=int(r["time"]),
            end=int(r["end_ts"]),
            kwh=_f(r.get("energy_net_kwh")),
            km_homy=_f(r.get("distance_km")),
            estimated=r.get("start_check") == "filled",
            waiting_sec=_f(r.get("waiting_sec")),
            waiting_kwh=_f(r.get("waiting_kwh")),
        )
        for r in rows
        if r.get("end_ts") is not None
    ]


def charges(db: Influx) -> list[Charge]:
    rows = db.query("SELECT * FROM ioniq_sessions WHERE kind = 'charge'")
    return [
        Charge(
            start=int(r["time"]),
            end=int(r["end_ts"]),
            kwh=_f(r.get("energy_in_kwh")),
            type=r.get("charge_type") or "unknown",
        )
        for r in rows
        if r.get("end_ts") is not None
    ]


def fixes(db: Influx, start: int, end: int) -> list[tuple[int, float, float]]:
    """Real GPS fixes: provider gps and accuracy 50 m or better. Read in windows."""
    out = []
    for t0 in range(start, end, GPS_WINDOW):
        t1 = min(t0 + GPS_WINDOW, end)
        rows = db.query(
            "SELECT lat, lon FROM ioniq WHERE \"group\" = 'gps' AND provider = 'gps'"
            f" AND acc_m <= 50 AND time >= {t0}ms AND time < {t1}ms"
        )
        out += [(int(r["time"]), r["lat"], r["lon"]) for r in rows if r["lat"] is not None]
    return out


def odometer(db: Influx, start: int, end: int) -> list[tuple[int, float]]:
    """The last odometer reading of each hour."""
    rows = db.query(
        "SELECT last(km) FROM ioniq WHERE \"group\" = 'odometer'"
        f" AND time >= {start}ms AND time < {end}ms GROUP BY time(1h) fill(none)"
    )
    return [(int(r["time"]), float(r["last"])) for r in rows if r["last"] is not None]
