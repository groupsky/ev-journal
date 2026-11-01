"""Distances and track thinning."""

import math

EARTH_M = 6_371_000.0


def dist_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_M * math.asin(math.sqrt(a))


def thin(fixes: list, min_m: float) -> list:
    """Keep a fix only when it is min_m or more from the last kept one. Keep the last fix.

    fixes are (t, lat, lon) tuples in time order.
    """
    if not fixes:
        return []
    kept = [fixes[0]]
    for f in fixes[1:]:
        if dist_m(kept[-1][1], kept[-1][2], f[1], f[2]) >= min_m:
            kept.append(f)
    if kept[-1] is not fixes[-1]:
        kept.append(fixes[-1])
    return kept


def track_km(fixes: list, min_step_m: float = 20.0) -> float:
    """Length of a track in km. Steps under min_step_m are merged, so GPS jitter
    while standing still does not add distance."""
    pts = thin(fixes, min_step_m)
    return sum(dist_m(a[1], a[2], b[1], b[2]) for a, b in zip(pts, pts[1:], strict=False)) / 1000
