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
            soc_start=_f(r.get("soc_start")),
            soc_end=_f(r.get("soc_end")),
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


def counters(db: Influx, start: int, end: int) -> list[tuple[int, float | None, float | None]]:
    """Battery lifetime counters: (t, cum_out_kwh, cum_in_kwh)."""
    rows = db.query(
        "SELECT cum_out_kwh, cum_in_kwh FROM ioniq WHERE \"group\" = 'bms/2101'"
        f" AND time >= {start}ms AND time <= {end}ms"
    )
    return [(int(r["time"]), _f(r["cum_out_kwh"]), _f(r["cum_in_kwh"])) for r in rows]


def odometer(db: Influx, start: int, end: int) -> list[tuple[int, float]]:
    """The last odometer reading of each hour."""
    rows = db.query(
        "SELECT last(km) FROM ioniq WHERE \"group\" = 'odometer'"
        f" AND time >= {start}ms AND time < {end}ms GROUP BY time(1h) fill(none)"
    )
    return [(int(r["time"]), float(r["last"])) for r in rows if r["last"] is not None]


def home_costs(db: Influx) -> dict[int, float]:
    """Cost of each home charge from ioniq_charges, by charge start (ms).

    Its rows start at the same time as the charge rows of ioniq_sessions.
    """
    rows = db.query("SELECT cost_eur FROM ioniq_charges WHERE place = 'home'")
    return {int(r["time"]): float(r["cost_eur"]) for r in rows if r.get("cost_eur") is not None}


def meter_minutes(db: Influx, start: int, end: int) -> list[tuple[int, float | None, float | None]]:
    """House charger meter per minute: (t, max power W, last total kWh)."""
    rows = db.query(
        "SELECT max(ap) AS w, last(tot_act) AS e FROM xymd1 WHERE \"device.name\" = 'charger'"
        f" AND time >= {start}ms AND time < {end}ms GROUP BY time(1m) fill(none)"
    )
    return [(int(r["time"]), _f(r["w"]), _f(r["e"])) for r in rows]


def last_soc(db: Influx, before: int) -> float | None:
    """The car's last SoC reading in the week before a time."""
    rows = db.query(
        "SELECT last(soc) FROM ioniq WHERE \"group\" = 'bms/2101'"
        f" AND time >= {before - GPS_WINDOW}ms AND time < {before}ms"
    )
    return _f(rows[0]["last"]) if rows else None
