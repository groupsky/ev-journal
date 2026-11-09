"""The GPS track of a journey for a map, with kWh/100 km per part (#30)."""

import bisect

from ev_journal.geo import dist_m, thin

STEP_M = 50.0  # the same thinning as ev_track
# Two counter readings further apart than this are a gap: no kWh across it.
COUNTER_GAP_MS = 60_000


class Energy:
    """Net battery kWh used (cum_out_kwh - cum_in_kwh) over time.

    readings are (t, cum_out_kwh, cum_in_kwh) in time order. The counters have
    0.1 kWh steps. So the curve goes through the readings where the value
    changes, and is a straight line between them. This spreads each step over
    the metres that used it, and a 50 m part does not jump between 0 and a
    whole step.
    """

    def __init__(self, readings: list):
        rs = [(t, round(o - i, 3)) for t, o, i in readings if o is not None and i is not None]
        self.times = [r[0] for r in rs]
        gaps = [
            (a[0], b[0]) for a, b in zip(rs, rs[1:], strict=False) if b[0] - a[0] > COUNTER_GAP_MS
        ]
        self.gap_starts = [g[0] for g in gaps]
        self.gap_ends = [g[1] for g in gaps]
        # Knots: the first and last reading, each change, and both sides of a
        # gap, so the line never runs across a gap.
        edges = {t for g in gaps for t in g}
        knots = [
            r
            for k, r in enumerate(rs)
            if k in (0, len(rs) - 1) or r[1] != rs[k - 1][1] or r[0] in edges
        ]
        self.kt = [k[0] for k in knots]
        self.kv = [k[1] for k in knots]

    def used(self, t0: int, t1: int) -> float | None:
        """kWh used from t0 to t1, or None when the readings do not cover it."""
        if not self.times or t0 < self.times[0] or t1 > self.times[-1]:
            return None
        # Gaps do not overlap, so the last one that starts before t1 decides.
        g = bisect.bisect_left(self.gap_starts, t1)
        if g and self.gap_ends[g - 1] > t0:
            return None
        return self._at(t1) - self._at(t0)

    def _at(self, t: int) -> float:
        k = bisect.bisect_right(self.kt, t)
        if k == len(self.kt):
            return self.kv[-1]
        t0, v0, t1, v1 = self.kt[k - 1], self.kv[k - 1], self.kt[k], self.kv[k]
        return v0 + (v1 - v0) * (t - t0) / (t1 - t0)


def journey_track(fixes: list, readings: list) -> list[dict]:
    """Points (t, lat, lon) thinned like ev_track, each with kWh/100 km of the
    part since the point before (None for the first point and across a gap)."""
    pts = thin(fixes, STEP_M)
    energy = Energy(readings)
    out = []
    for k, p in enumerate(pts):
        rate = None
        if k:
            a = pts[k - 1]
            kwh, m = energy.used(a[0], p[0]), dist_m(a[1], a[2], p[1], p[2])
            if kwh is not None and m > 0:
                rate = kwh / m * 100_000
        out.append({"t": p[0], "lat": p[1], "lon": p[2], "kwh_per_100km": rate})
    return out
