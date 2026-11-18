"""Outside calls for the car, so the car holds no third-party keys (#70).

The road weather is roadcheck.at_points. Here: Google Routes and the rate
limit per token.
"""

import threading
from datetime import UTC, datetime

from ev_journal.roadcheck import Client

ROUTES = "https://routes.googleapis.com/directions/v2:computeRoutes"
ROUTES_FIELDS = "routes.duration,routes.distanceMeters"  # small mask: small cost


def _point(lat: float, lon: float) -> dict:
    return {"location": {"latLng": {"latitude": lat, "longitude": lon}}}


def routes(
    client: Client,
    key: str,
    origin: tuple[float, float],
    destination: tuple[float, float],
    departure_ms: int | None = None,
) -> dict:
    """Duration with traffic (s) and distance (m) of the first route; both
    None when Google finds no route."""
    body = {
        "origin": _point(*origin),
        "destination": _point(*destination),
        "travelMode": "DRIVE",
        "routingPreference": "TRAFFIC_AWARE",
    }
    if departure_ms is not None:
        t = datetime.fromtimestamp(departure_ms / 1000, UTC)
        body["departureTime"] = t.isoformat().replace("+00:00", "Z")
    headers = {"X-Goog-Api-Key": key, "X-Goog-FieldMask": ROUTES_FIELDS}
    _h, data = client.get(ROUTES, body, "google-routes", 1, 1, headers)
    found = (data or {}).get("routes") or [{}]
    first = found[0]
    dur = first.get("duration")  # "123s"
    return {
        "duration_s": int(float(dur.rstrip("s"))) if dur else None,
        "distance_m": first.get("distanceMeters"),
    }


class RateLimit:
    """Calls per minute and per UTC day, per token. Fixed windows."""

    def __init__(self, per_min: int, per_day: int):
        self.per_min, self.per_day = per_min, per_day
        self._seen: dict[tuple, int] = {}
        self._lock = threading.Lock()

    def take(self, who: str, now_s: float) -> int | None:
        """Count one call; None when it is allowed, else the seconds to wait."""
        minute, day = int(now_s // 60), int(now_s // 86_400)
        keys = ((who, "m", minute), (who, "d", day))
        with self._lock:
            now = {"m": minute, "d": day}
            self._seen = {k: v for k, v in self._seen.items() if k[2] == now[k[1]]}
            if self._seen.get(keys[1], 0) >= self.per_day:
                return int((day + 1) * 86_400 - now_s) + 1
            if self._seen.get(keys[0], 0) >= self.per_min:
                return int((minute + 1) * 60 - now_s) + 1
            for k in keys:
                self._seen[k] = self._seen.get(k, 0) + 1
        return None
