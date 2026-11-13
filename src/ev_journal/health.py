"""Daily battery health series and the fan table (#49).

Rules and reasons: docs/design.md ("Health series").
"""

import json
import sqlite3
import statistics
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

PARK_MS = 6 * 3_600_000  # a park of at least this long before a 12 V reading
QUIET_A = 5.0  # cell spread median only over frames with |hv_a| below this

# Physical bounds. A value outside them is a garbage frame and is dropped.
# Cell voltage and module temperature are the bounds of homy's
# ioniq-cell-health bot; the others are plain sanity limits.
CELL_V = (1.0, 5.0)
MODULE_SPREAD_C = (0.0, 140.0)  # module temperatures -40..100 °C
SOH = (50.0, 110.0)
ISOLATION_KOHM = (1.0, 10_000.0)  # 0 is "not measured"
AUX_V = (8.0, 16.0)
FAN_FEEDBACK = (0.0, 255.0)
FAN_STEPS = range(1, 10)

FIELDS = (
    "soh_max",
    "isolation_min_kohm",
    "cell_spread_mv",
    "cell_spread_mv_max",
    "module_temp_spread_c_max",
    "aux_12v_rest",
)


def init(db: sqlite3.Connection) -> None:
    """data is NULL for a past day without car data, so it is not read again."""
    db.execute("CREATE TABLE IF NOT EXISTS health_days (day TEXT PRIMARY KEY, data TEXT)")


@dataclass
class Frames:
    """Raw car frames of one time window."""

    bms: list[dict] = field(default_factory=list)  # bms/2101 rows with "time"
    soh: list[tuple[int, float | None]] = field(default_factory=list)  # bms/2105
    module_spread: list[tuple[int, float | None]] = field(default_factory=list)
    last_aux_before: int | None = None  # time of the last valid 12 V reading before


def _in(v, bounds: tuple[float, float]) -> bool:
    return v is not None and bounds[0] <= v <= bounds[1]


def summarize(fr: Frames, tz: ZoneInfo) -> dict[str, dict]:
    """One row per local day with any frame: the fields of FIELDS and
    "fan": {step: {feedback: count}}."""
    acc: dict[str, dict] = {}

    def day(t: int) -> dict:
        key = datetime.fromtimestamp(t / 1000, tz).date().isoformat()
        return acc.setdefault(
            key, {"soh": [], "iso": [], "quiet": [], "all": [], "mod": [], "aux": None, "fan": {}}
        )

    for t, v in fr.soh:
        d = day(t)
        if _in(v, SOH):
            d["soh"].append(v)
    for t, v in fr.module_spread:
        d = day(t)
        if _in(v, MODULE_SPREAD_C):
            d["mod"].append(v)
    last_aux = fr.last_aux_before
    for r in sorted(fr.bms, key=lambda r: r["time"]):
        t = int(r["time"])
        d = day(t)
        if _in(r.get("isolation_kohm"), ISOLATION_KOHM):
            d["iso"].append(r["isolation_kohm"])
        hi, lo = r.get("cell_max_v"), r.get("cell_min_v")
        if _in(hi, CELL_V) and _in(lo, CELL_V) and hi >= lo:
            mv = (hi - lo) * 1000
            d["all"].append(mv)
            if r.get("hv_a") is not None and abs(r["hv_a"]) < QUIET_A:
                d["quiet"].append(mv)
        aux = r.get("aux_12v")
        if _in(aux, AUX_V):
            if d["aux"] is None and (last_aux is None or t - last_aux >= PARK_MS):
                d["aux"] = aux
            last_aux = t
        step, fb = r.get("fan_status"), r.get("fan_feedback")
        if step is not None and int(step) == step and int(step) in FAN_STEPS:
            if _in(fb, FAN_FEEDBACK):
                h = d["fan"].setdefault(str(int(step)), {})
                k = str(round(fb))
                h[k] = h.get(k, 0) + 1

    out = {}
    for key, d in acc.items():
        out[key] = {
            "soh_max": max(d["soh"], default=None),
            "isolation_min_kohm": min(d["iso"], default=None),
            "cell_spread_mv": round(statistics.median(d["quiet"]), 1) if d["quiet"] else None,
            "cell_spread_mv_max": round(max(d["all"]), 1) if d["all"] else None,
            "module_temp_spread_c_max": max(d["mod"], default=None),
            "aux_12v_rest": d["aux"],
            "fan": d["fan"],
        }
    return out


def _median_of(hist: dict[str, int]) -> float:
    return statistics.median(float(k) for k, n in hist.items() for _ in range(n))


class Health:
    """Reads days from InfluxDB once and keeps past days in SQLite.

    read(start_ms, end_ms) gives the Frames of a window. Today and
    yesterday are always read again and never stored.
    """

    def __init__(
        self,
        db_path: Path,
        tz: ZoneInfo,
        read: Callable[[int, int], Frames],
        now: Callable[[], float] = time.time,
    ):
        self.db_path, self.tz, self.read, self.now = db_path, tz, read, now
        self.lock = threading.Lock()

    def _ms(self, d: date) -> int:
        return int(datetime(d.year, d.month, d.day, tzinfo=self.tz).timestamp() * 1000)

    def _read(self, first: date, last: date) -> dict[str, dict]:
        return summarize(self.read(self._ms(first), self._ms(last + timedelta(days=1))), self.tz)

    def today(self) -> date:
        return datetime.fromtimestamp(self.now(), self.tz).date()

    def _days(self, first: date, last: date) -> dict[str, dict]:
        """Rows of days first..last that have car data."""
        today = self.today()
        last = min(last, today)
        fresh = today - timedelta(days=1)  # from here on: read again, not stored
        out: dict[str, dict] = {}
        with self.lock:
            conn = sqlite3.connect(self.db_path)
            try:
                init(conn)
                have = dict(
                    conn.execute(
                        "SELECT day, data FROM health_days WHERE day BETWEEN ? AND ?",
                        (first.isoformat(), last.isoformat()),
                    ).fetchall()
                )
                missing = [
                    first + timedelta(days=i)
                    for i in range((min(last, fresh - timedelta(days=1)) - first).days + 1)
                    if (first + timedelta(days=i)).isoformat() not in have
                ]
                months: dict[tuple[int, int], list[date]] = {}
                for d in missing:
                    months.setdefault((d.year, d.month), []).append(d)
                for ds in months.values():  # one read per month
                    got = self._read(ds[0], ds[-1])
                    with conn:
                        for d in ds:
                            row = got.get(d.isoformat())
                            have[d.isoformat()] = json.dumps(row) if row else None
                            conn.execute(
                                "INSERT OR REPLACE INTO health_days VALUES (?, ?)",
                                (d.isoformat(), have[d.isoformat()]),
                            )
            finally:
                conn.close()
        for key, data in have.items():
            if data:
                out[key] = json.loads(data)
        if last >= fresh:
            got = self._read(max(first, fresh), last)
            out.update({k: v for k, v in got.items() if first.isoformat() <= k <= last.isoformat()})
        return dict(sorted(out.items()))

    def daily(self, first: date, last: date) -> list[dict]:
        """One row per local day with car data, oldest first."""
        return [{"day": k, **{f: v[f] for f in FIELDS}} for k, v in self._days(first, last).items()]

    def fan(self, months: int) -> list[dict]:
        """Per month and fan step: frame count and median fan_feedback,
        over the last `months` months with this one, oldest first."""
        today = self.today()
        y, m = today.year, today.month - (months - 1)
        while m < 1:
            y, m = y - 1, m + 12
        hists: dict[tuple[str, int], dict[str, int]] = {}
        for key, row in self._days(date(y, m, 1), today).items():
            for step, h in row["fan"].items():
                acc = hists.setdefault((key[:7], int(step)), {})
                for k, n in h.items():
                    acc[k] = acc.get(k, 0) + n
        return [
            {
                "month": month,
                "step": step,
                "count": sum(h.values()),
                "fan_feedback_median": _median_of(h),
            }
            for (month, step), h in sorted(hists.items())
        ]
