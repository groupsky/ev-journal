"""Home charges from the old OVMS daily export zips (#62).

The OVMS module sent data until 2026-03. Its server mailed one zip per day
with CSV files. This module reads them (no unpacking to disk), finds the
charges and stores them in the table ovms_charges. They teach only the
level rates and their months (forecast.levels(), level_rates()).

Rules and the numbers behind them: docs/home-charge.md, "OVMS exports".
"""

import bisect
import csv
import io
import sqlite3
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from ev_journal import forecast, homecharge, places
from ev_journal.journeys import month_key

TZ = ZoneInfo("Europe/Berlin")  # rec_time is in this zone (rule 1)
MIN_POWER_KW = 0.3  # a charging row counts only above this chargepower (rule 4)
MAX_PAUSE_MS = 3 * 3_600_000  # a stopped pause shorter than this joins two parts (rule 5)
MAX_ROW_GAP_MS = 3_600_000  # rows come every 1–10 min; a longer gap (a lost day) ends a part
NEAR_MS = 3_600_000  # a temperature is read this close to the start
GPS_BEFORE_MS = 6 * 3_600_000  # the GPS fix of a charge is from this long before it, or in it
CHARGING = ("charging", "topoff")
MIN_GAIN = 5.0  # a charge is kept with at least this many BMS %
MIN_HOURS = 1.0  # and this many hours at power
METER_LEAD_MS = 5 * 60_000

SCHEMA = """
CREATE TABLE IF NOT EXISTS ovms_charges (start INTEGER PRIMARY KEY, end INTEGER NOT NULL,
  hours REAL NOT NULL, soc_start REAL NOT NULL, soc_end REAL NOT NULL, power_kw REAL,
  ambient_c REAL, battery_c REAL, at_home INTEGER NOT NULL, meter_kwh REAL);
"""


def init(db: sqlite3.Connection) -> None:
    db.executescript(SCHEMA)


def bms(display: float, disp: tuple[float, float] = forecast.DISPLAY) -> float:
    """The BMS % of an OVMS soc. OVMS has one SoC only; it is taken as the
    car-screen % and turned back with the inverse of the #36 map (rule 7)."""
    return (display - disp[0]) / disp[1]


def _num(v: str | None) -> float | None:
    try:
        return float(v) if v not in (None, "") else None
    except ValueError:
        return None


def _ms(texts: list[str]) -> list[int | None]:
    """UTC ms of the rec_time values of one file, in file order. In the
    October hour that repeats, a time that goes back sets fold=1 (rule 1).
    A time that does not exist (the March hour that is skipped) gives None."""
    out, prev, fold = [], None, 0
    for t in texts:
        dt = datetime.fromisoformat(t)
        aware = dt.replace(tzinfo=TZ)
        if aware.astimezone(UTC).astimezone(TZ).replace(tzinfo=None) != dt:
            out.append(None)
            continue
        if aware.utcoffset() == dt.replace(fold=1, tzinfo=TZ).utcoffset():
            fold = 0
        elif prev is not None and dt < prev:
            fold = 1
        prev = dt
        out.append(int(dt.replace(tzinfo=TZ, fold=fold).timestamp() * 1000))
    return out


def read_dir(path: Path) -> dict[str, list[dict]]:
    """Rows of s.csv, d.csv and l.csv of all zips in a folder, each sorted
    by time, with "t" in UTC ms. Repeated times are kept once (rule 2);
    rows with soc or odometer 0 are dropped (rule 3)."""
    out: dict[str, dict[int, dict]] = {"s": {}, "d": {}, "l": {}}
    for z in sorted(path.glob("*.zip")):
        with zipfile.ZipFile(z) as zf:
            names = {Path(n).name: n for n in zf.namelist()}
            for key in out:
                if f"{key}.csv" not in names:
                    continue
                text = zf.read(names[f"{key}.csv"]).decode("utf-8", "replace")
                rows = [r for r in csv.DictReader(io.StringIO(text)) if r.get("rec_time")]
                for r, t in zip(rows, _ms([r["rec_time"] for r in rows]), strict=True):
                    if t is None:
                        continue
                    if key == "s" and _num(r.get("soc")) == 0:
                        continue
                    if key == "d" and _num(r.get("odometer")) == 0:
                        continue
                    out[key].setdefault(t, {**r, "t": t})
    return {k: [v[t] for t in sorted(v)] for k, v in out.items()}


@dataclass
class Charge:
    start: int  # the first charging row, UTC ms
    end: int  # the last charging row
    hours: float  # time at power: the parts, the pauses left out
    soc_start: float  # BMS %
    soc_end: float
    power_kw: float | None = None  # mean chargepower (battery side)
    ambient_c: float | None = None
    battery_c: float | None = None
    at_home: bool = False
    meter_kwh: float | None = None  # house charger meter; None before it or away

    @property
    def dsoc(self) -> float:
        return self.soc_end - self.soc_start


def _charging(r: dict) -> bool:
    kw = _num(r.get("chargepower"))
    return r.get("chargestate") in CHARGING and kw is not None and kw > MIN_POWER_KW


def _parts(s_rows: list[dict]) -> list[tuple[list[dict], list[dict] | None]]:
    """Runs of charging rows with power (rule 4), each with the rows of the
    pause before it. Any other row, or a gap of over 1 h, ends a run; after
    a gap the pause is None."""
    parts, cur, pause = [], [], []
    for r in s_rows:
        if _charging(r) and _num(r.get("soc")) is not None:
            if cur and r["t"] - cur[-1]["t"] > MAX_ROW_GAP_MS:
                parts.append((cur, pause))
                cur, pause = [], None  # no rows: the gap is unknown, never joined
            cur.append(r)
        else:
            if cur:
                parts.append((cur, pause))
                cur, pause = [], []
            if pause is not None:
                pause.append(r)
    if cur:
        parts.append((cur, pause))
    return parts


def _joins(last: list[dict], p: list[dict], pause: list[dict] | None) -> bool:
    """Rule 5: a pause shorter than 3 h, with only stopped rows (or ghost
    charging rows with no power), the SoC never below where it stopped, and
    rising after it."""
    soc_a = _num(last[-1]["soc"])
    return (
        pause is not None
        and p[0]["t"] - last[-1]["t"] < MAX_PAUSE_MS
        and all(r.get("chargestate") in ("stopped", *CHARGING) for r in pause)
        and all((_num(r.get("soc")) or soc_a) >= soc_a for r in pause)
        and _num(p[0]["soc"]) >= soc_a
        and _num(p[-1]["soc"]) > soc_a
    )


def charges(s_rows: list[dict]) -> list[Charge]:
    """Charges from the s.csv rows: parts joined by rule 5. SoC start, SoC
    end and hours come from the same span: the first and last charging rows
    (rule 6); the hours leave a joined pause out."""
    groups: list[list[list[dict]]] = []
    for p, pause in _parts(s_rows):
        if groups and _joins(groups[-1][-1], p, pause):
            groups[-1].append(p)
        else:
            groups.append([p])
    out = []
    for g in groups:
        first, last = g[0][0], g[-1][-1]
        rows = [r for p in g for r in p]
        c = Charge(
            first["t"],
            last["t"],
            sum(p[-1]["t"] - p[0]["t"] for p in g) / 3_600_000,
            bms(_num(first["soc"])),
            bms(_num(last["soc"])),
            sum(_num(r["chargepower"]) for r in rows) / len(rows),
        )
        if c.dsoc >= MIN_GAIN and c.hours >= MIN_HOURS:
            out.append(c)
    return out


def _near(rows: list[dict], ts: list[int], t: int, field: str) -> float | None:
    """The last value of a field in the hour up to t; else the first in the hour after it."""
    i = bisect.bisect_right(ts, t)
    for r in (*reversed(rows[:i]), *rows[i:]):
        if abs(r["t"] - t) > NEAR_MS:
            continue
        if (v := _num(r.get(field))) is not None:
            return v
    return None


def _pos(l_rows: list[dict], ts: list[int], c: Charge) -> tuple[float, float] | None:
    """The last GPS fix with a lock in the charge, or in the 6 h before it."""
    lo = bisect.bisect_left(ts, c.start - GPS_BEFORE_MS)
    for r in reversed(l_rows[lo : bisect.bisect_right(ts, c.end)]):
        lat, lon = _num(r.get("latitude")), _num(r.get("longitude"))
        if lat is not None and lon is not None and _num(r.get("gpslock")) != 0 and (lat or lon):
            return lat, lon
    return None


def load(path: Path, pl: list[places.Place], meter_minutes) -> list[Charge]:
    """The charges of the zips in a folder: temperatures from d.csv, home or
    away from the GPS of l.csv, and the meter kWh of a home charge.
    meter_minutes(start, end): source.meter_minutes for the time."""
    rows = read_dir(path)
    d_ts, l_ts = [r["t"] for r in rows["d"]], [r["t"] for r in rows["l"]]
    out = charges(rows["s"])
    for c in out:
        c.ambient_c = _near(rows["d"], d_ts, c.start, "ambient")
        c.battery_c = _near(rows["d"], d_ts, c.start, "tbattery")
        p = places.place_at(pl, _pos(rows["l"], l_ts, c))
        c.at_home = p is not None and p.kind == "home"
        if c.at_home:
            # All meter blocks in the span: a short last part is charging, not preheat.
            t0 = c.start - METER_LEAD_MS
            bs = homecharge.blocks(meter_minutes(t0, c.end))
            c.meter_kwh = sum(b.kwh for b in bs if b.end >= t0) or None
    return out


COLUMNS = (
    "start, end, hours, soc_start, soc_end, power_kw, ambient_c, battery_c, at_home, meter_kwh"
)


def save(db: sqlite3.Connection, cs: list[Charge]) -> None:
    """Replace all rows with the charges: the import reads the whole folder,
    so the same zips give the same rows, and a charge that moved or went
    away leaves no old row."""
    with db:
        db.execute("DELETE FROM ovms_charges")
        db.executemany(
            f"INSERT OR REPLACE INTO ovms_charges ({COLUMNS})"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    c.start,
                    c.end,
                    c.hours,
                    c.soc_start,
                    c.soc_end,
                    c.power_kw,
                    c.ambient_c,
                    c.battery_c,
                    int(c.at_home),
                    c.meter_kwh,
                )
                for c in cs
            ],
        )


def stored(db: sqlite3.Connection) -> list[Charge]:
    return [
        Charge(*r[:8], at_home=bool(r[8]), meter_kwh=r[9])
        for r in db.execute(f"SELECT {COLUMNS} FROM ovms_charges ORDER BY start")
    ]


def home_charges(cs: list[Charge], kwh_per_pct: float, tz) -> list[homecharge.HomeCharge]:
    """The home charges matched to the meter, as HomeCharge for the level
    rates only. Battery kWh = BMS % gained × kwh_per_pct."""
    return [
        homecharge.HomeCharge(
            c.start,
            c.meter_kwh,
            c.dsoc * kwh_per_pct,
            c.soc_start,
            c.soc_end,
            month_key(c.start, tz)[0],
            c.hours,
        )
        for c in cs
        if c.at_home and c.meter_kwh
    ]
