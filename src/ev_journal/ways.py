"""The usual way between two places, learned from journey tracks (#54).

Each journey from A to B is a GPS track. Two tracks take the same way when
almost every point of each lies near the other. The way with the most
journeys wins, if it has enough of them. Its line is the track of the
journey that agrees with the most others, simplified.
"""

import math
from datetime import datetime
from zoneinfo import ZoneInfo

from ev_journal.geo import EARTH_M, thin

MIN_JOURNEYS = 3  # a way needs this many journeys
MIN_SHARE = 0.5  # and this share of the journeys from A to B
NEAR_M = 150.0  # a point this near the other track is on it
SAME_SHARE = 0.9  # this share of points of each track on the other: the same way
SIMPLIFY_M = 20.0
STEP_M = 50.0  # tracks are compared at one point per this many metres


def _cells(pts: list, ref_lat: float) -> set[tuple[int, int]]:
    """Grid cells of NEAR_M size that hold a point."""
    dlat = math.degrees(NEAR_M / EARTH_M)
    dlon = dlat / max(math.cos(math.radians(ref_lat)), 0.01)
    return {(math.floor(p[1] / dlat), math.floor(p[2] / dlon)) for p in pts}


def _on(cells: list[tuple[int, int]], other: set[tuple[int, int]]) -> float:
    """Share of cells with a cell of the other track in their 3 x 3 block:
    a point within NEAR_M of the other track always counts."""
    if not cells:
        return 0.0
    hit = sum(
        1 for x, y in cells if any((x + i, y + j) in other for i in (-1, 0, 1) for j in (-1, 0, 1))
    )
    return hit / len(cells)


def same_way(a: set, b: set) -> bool:
    return _on(list(a), b) >= SAME_SHARE and _on(list(b), a) >= SAME_SHARE


def simplify(pts: list, tol_m: float = SIMPLIFY_M) -> list:
    """Douglas-Peucker on (t, lat, lon) points: drop points closer than tol_m
    to the line between the kept ones."""
    if len(pts) < 3:
        return list(pts)
    ref = math.cos(math.radians(pts[0][1]))

    def xy(p):
        return (math.radians(p[2]) * ref * EARTH_M, math.radians(p[1]) * EARTH_M)

    xs = [xy(p) for p in pts]
    keep = [False] * len(pts)
    keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    while stack:
        i, k = stack.pop()
        (ax, ay), (bx, by) = xs[i], xs[k]
        dx, dy = bx - ax, by - ay
        n = math.hypot(dx, dy)
        best, far = -1.0, -1
        for j in range(i + 1, k):
            px, py = xs[j]
            if n == 0:
                d = math.hypot(px - ax, py - ay)
            else:
                d = abs(dx * (ay - py) - dy * (ax - px)) / n
            if d > best:
                best, far = d, j
        if best > tol_m:
            keep[far] = True
            stack += [(i, far), (far, k)]
    return [p for p, k in zip(pts, keep, strict=True) if k]


def usual_way(
    rows: list[dict],
    from_id: str,
    to_id: str,
    tz: ZoneInfo,
    min_journeys: int = MIN_JOURNEYS,
    min_share: float = MIN_SHARE,
) -> dict:
    """rows: journeys as {"start": ms, "from": place id, "to": place id,
    "fixes": [(t, lat, lon)]}. Journeys without fixes do not count.

    Returns the way with its support, or found False and no line."""
    js = [r for r in rows if r["from"] == from_id and r["to"] == to_id and len(r["fixes"]) > 1]
    out = {"from": from_id, "to": to_id, "journeys": len(js), "found": False}
    out |= {"support": 0, "share": 0.0, "first": None, "last": None, "line": None}
    if not js:
        return out
    ref = js[0]["fixes"][0][1]
    cells = [_cells(thin(j["fixes"], STEP_M), ref) for j in js]
    same = [[i for i in range(len(js)) if same_way(cells[k], cells[i])] for k in range(len(js))]
    # The most supported way; on a tie the newest journey's way.
    k = max(range(len(js)), key=lambda k: (len(same[k]), js[k]["start"]))
    group = same[k]
    share = len(group) / len(js)
    days = sorted(datetime.fromtimestamp(js[i]["start"] / 1000, tz).date() for i in group)
    out |= {"support": len(group), "share": round(share, 3)}
    out |= {"first": days[0].isoformat(), "last": days[-1].isoformat()}
    if len(group) < min_journeys or share < min_share:
        return out
    line = simplify(js[k]["fixes"])
    out["found"] = True
    coords = [[round(p[2], 6), round(p[1], 6)] for p in line]
    out["line"] = {"type": "LineString", "coordinates": coords}
    return out
