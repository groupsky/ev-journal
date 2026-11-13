"""Read the inputs from InfluxDB (read user, SELECT only)."""

from ev_journal.influx import Influx
from ev_journal.journeys import Charge, Trip

DAY = 86_400_000
HOUR = 3_600_000
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
    """Battery lifetime counters: (t, cum_out_kwh, cum_in_kwh). Read in
    windows of an hour, so a long journey stays under the row limit."""
    out = []
    for t0 in range(start, end + 1, HOUR):
        t1 = min(t0 + HOUR, end + 1)
        rows = db.query(
            "SELECT cum_out_kwh, cum_in_kwh FROM ioniq WHERE \"group\" = 'bms/2101'"
            f" AND time >= {t0}ms AND time < {t1}ms"
        )
        out += [(int(r["time"]), _f(r["cum_out_kwh"]), _f(r["cum_in_kwh"])) for r in rows]
    return out


def odometer(db: Influx, start: int, end: int) -> list[tuple[int, float]]:
    """The last odometer reading of each hour."""
    rows = db.query(
        "SELECT last(km) FROM ioniq WHERE \"group\" = 'odometer'"
        f" AND time >= {start}ms AND time < {end}ms GROUP BY time(1h) fill(none)"
    )
    return [(int(r["time"]), float(r["last"])) for r in rows if r["last"] is not None]


def last_odometer(db: Influx) -> tuple[int, float] | None:
    """The car's last odometer reading: (t, km), or None if there is none (#44)."""
    rows = db.query("SELECT last(km) FROM ioniq WHERE \"group\" = 'odometer'")
    if not rows or rows[0]["last"] is None:
        return None
    return int(rows[0]["time"]), float(rows[0]["last"])


def outside_temps(db: Influx, start: int, tz: str) -> list[float]:
    """One mean outside temperature (°C) per local day since start, oldest
    first, from the car's ambient and hvac readings (#45). A day with no
    reading is left out."""
    days: dict[int, list[float]] = {}
    for group, field in (("ambient", "c"), ("hvac", "outdoor_c")):
        rows = db.query(
            f"SELECT mean({field}) FROM ioniq WHERE \"group\" = '{group}'"
            f" AND time >= {start}ms GROUP BY time(1d) fill(none) tz('{tz}')"
        )
        for r in rows:
            if r["mean"] is not None:
                days.setdefault(int(r["time"]), []).append(float(r["mean"]))
    return [sum(v) / len(v) for _t, v in sorted(days.items())]


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


def soc_pairs(db: Influx, start: int) -> list[tuple[float, float]]:
    """(BMS %, car screen %) per hour since start: the last soc of bms/2101
    and the last soc_display of bms/2105 in the same hour (#36)."""

    def hourly(group: str, field: str) -> dict[int, float]:
        rows = db.query(
            f"SELECT last({field}) FROM ioniq WHERE \"group\" = '{group}'"
            f" AND time >= {start}ms GROUP BY time(1h) fill(none)"
        )
        return {int(r["time"]): float(r["last"]) for r in rows if r["last"] is not None}

    bms, screen = hourly("bms/2101", "soc"), hourly("bms/2105", "soc_display")
    return [(bms[t], screen[t]) for t in sorted(bms.keys() & screen.keys())]


MOVING_KPH = 3.0  # as homy's ioniq-sessions: above this the car drives


def moved_after(db: Influx, t: int) -> bool:
    """A car frame after t shows driving speed: a trip runs or ran (homy
    writes a trip row only when the trip ends)."""
    rows = db.query(
        f"SELECT speed_kph FROM ioniq WHERE speed_kph > {MOVING_KPH} AND time > {t}ms LIMIT 1"
    )
    return bool(rows)


def plug_frames(db: Influx, start: int) -> list[tuple[int, bool]]:
    """The last car frame since start of each kind that shows the cable:
    (time, cable in). bcm_b00e charge_connector, bms/2101 ac_port or dc_port."""
    out = []
    for group, fields in (("bcm_b00e", ["charge_connector"]), ("bms/2101", ["ac_port", "dc_port"])):
        rows = db.query(
            f"SELECT {', '.join(fields)} FROM ioniq WHERE \"group\" = '{group}'"
            f" AND time > {start}ms ORDER BY time DESC LIMIT 1"
        )
        for r in rows:
            vals = [r.get(f) for f in fields if r.get(f) is not None]
            if vals:
                out.append((int(r["time"]), any(float(v) == 1 for v in vals)))
    return out


def health_frames(db: Influx, start: int, end: int):
    """Raw frames for the health series in [start, end) (#49)."""
    from ev_journal.health import AUX_V, Frames

    w = f" AND time >= {start}ms AND time < {end}ms"
    bms = db.query(
        "SELECT isolation_kohm, cell_max_v, cell_min_v, hv_a, aux_12v, fan_status, fan_feedback"
        " FROM ioniq WHERE \"group\" = 'bms/2101'" + w
    )
    soh = db.query("SELECT soh FROM ioniq WHERE \"group\" = 'bms/2105'" + w)
    spread = db.query(
        "SELECT value FROM ioniq WHERE \"group\" = 'derived/module_temp_spread_c'" + w
    )
    # No valid 12 V reading in the 30 days before counts as a long park.
    prev = db.query(
        "SELECT last(aux_12v) FROM ioniq WHERE \"group\" = 'bms/2101'"
        f" AND aux_12v >= {AUX_V[0]} AND aux_12v <= {AUX_V[1]}"
        f" AND time >= {start - 30 * DAY}ms AND time < {start}ms"
    )
    return Frames(
        bms=[{k: (v if k == "time" else _f(v)) for k, v in r.items()} for r in bms],
        soh=[(int(r["time"]), _f(r["soh"])) for r in soh],
        module_spread=[(int(r["time"]), _f(r["value"])) for r in spread],
        last_aux_before=int(prev[0]["time"]) if prev else None,
    )
