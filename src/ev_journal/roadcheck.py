"""Road check before the next likely trip (#55).

A set time before the next likely trip, ask the road weather along its usual
way: Xweather road weather where it has road data, a free Open-Meteo
estimate where it has none. The result has the worst level and the points
that are not GREEN, by km from the start. No place or road names.
"""

import json
import sys
import time
import urllib.parse
from dataclasses import dataclass
from datetime import datetime

from ev_journal.geo import dist_m

XWEATHER = "https://data.api.xweather.com/roadweather"
OPEN_METEO = "https://api.open-meteo.com/v1/forecast"
LEVELS = ("GREEN", "YELLOW", "RED")
STEP_M = 1000.0  # one point per km along the way
MINDIST = "100m"  # always sent: Xweather skips points closer than 1 km by default
QUOTA_HEADER = "x-ratelimit-remaining-period"
DETAIL_COST = 5  # roadweather/conditions costs 5 accesses per point
METEO_BATCH = 50  # points per Open-Meteo call


@dataclass(frozen=True)
class Rules:
    """The estimate rules where Xweather has no road data. The car app uses
    the same first rules; keep them in step."""

    ice_yellow_c: float = 2.0  # air at or below this, with wet or near dew point
    ice_red_c: float = 0.0  # air at or below this, with precipitation
    dew_gap_c: float = 1.0  # dew point within this of the air
    precip_hours: int = 6
    fog_yellow_m: float = 1000.0  # visibility under this
    fog_red_m: float = 200.0


def due_at(trip: dict, lead_min: int) -> int | None:
    """When to check for this next trip (ways.next_trip), ms; None: no
    likely trip, or no usual way to check."""
    if not trip.get("found") or not trip.get("way"):
        return None
    return int(datetime.fromisoformat(trip["start"]).timestamp() * 1000) - lead_min * 60_000


def trip_key(trip: dict) -> tuple:
    return (trip["from"], trip["to"], trip["start"])


def points_along(coords: list, step_m: float = STEP_M) -> list[tuple[float, float, float]]:
    """(km from the start, lat, lon) every step_m along a GeoJSON line of
    [lon, lat], with the first and the last point."""
    if not coords:
        return []
    out = [(0.0, coords[0][1], coords[0][0])]
    done, nxt = 0.0, step_m
    for (lon1, lat1), (lon2, lat2) in zip(coords, coords[1:], strict=False):
        d = dist_m(lat1, lon1, lat2, lon2)
        while d > 0 and nxt <= done + d:
            f = (nxt - done) / d
            out.append((nxt / 1000, lat1 + f * (lat2 - lat1), lon1 + f * (lon2 - lon1)))
            nxt += step_m
        done += d
    if done - out[-1][0] * 1000 > step_m / 2:
        out.append((done / 1000, coords[-1][1], coords[-1][0]))
    return out


class Client:
    """HTTP calls with the outside keys. Errors never carry a key. Each call
    writes one log line: API, size, cost, quota left and time; never the
    URL or the body, so no coordinates and no keys (#70)."""

    def __init__(self, fetch, client_id: str | None, secret: str | None, *more_keys: str):
        self._fetch, self._id, self._secret = fetch, client_id, secret
        self._keys = [k for k in (secret, client_id, *more_keys) if k]

    def get(
        self,
        url: str,
        body: list | dict | None = None,
        api: str = "?",
        n: int = 1,
        cost: int | None = None,
        headers: dict | None = None,
    ) -> tuple[dict, object]:
        t0, left, ok = time.monotonic(), None, "failed"
        try:
            if headers:
                got, data = self._fetch(url, body, headers)
            else:
                got, data = self._fetch(url, body)
            got = {k.lower(): v for k, v in got.items()}
            left, ok = _quota(got), "ok"
            return got, data
        except Exception as e:
            raise RuntimeError(self._clean(f"{type(e).__name__}: {e}")) from None
        finally:
            ms = round((time.monotonic() - t0) * 1000)
            print(
                f"ev-journal: call {api} {ok} n={n} cost={n if cost is None else cost}"
                f" quota_left={left} ms={ms}",
                file=sys.stderr,
            )

    def xweather(self, path: str, body: list, cost: int = 1) -> tuple[dict, object]:
        q = urllib.parse.urlencode(
            {"client_id": self._id, "client_secret": self._secret, "mindist": MINDIST}
        )
        api = "xweather" + (path.replace("/", "-") or "-summary")
        return self.get(f"{XWEATHER}{path}/route?{q}", body, api, len(body), cost * len(body))

    def _clean(self, text: str) -> str:
        for s in self._keys:
            text = text.replace(s, "***").replace(urllib.parse.quote(s, safe=""), "***")
        return text


def _quota(headers: dict) -> int | None:
    v = headers.get(QUOTA_HEADER)
    try:
        return int(v) if v is not None else None
    except ValueError:
        return None


def _period(feature: dict, at_s: float) -> dict | None:
    """The forecast period nearest at_s, or None when there is no road data."""
    props = feature.get("properties") or {}
    req = props.get("request") or {}
    if req.get("skipped") or req.get("success") is False:
        return None
    resp = props.get("response")
    if isinstance(resp, list):
        resp = resp[0] if resp else None
    periods = (resp or {}).get("periods") or []
    if not periods:
        return None
    return min(periods, key=lambda p: abs(p.get("timestamp", 0) - at_s))


def _road_body(pts: list, times: list[float], now_ms: int) -> list[dict]:
    return [
        {"p": f"{lat:.6f},{lon:.6f}", "id": str(i), "from": f"+{_mins(t, now_ms)}minutes"}
        for i, ((_km, lat, lon), t) in enumerate(zip(pts, times, strict=True))
    ]


def _mins(t_ms: float, now_ms: int) -> int:
    return max(0, round((t_ms - now_ms) / 60_000))


def _by_id(data: object) -> dict[str, dict]:
    """Route features by the id sent: the feature id, else the request id,
    else the position (the answer keeps the order of the points). A route
    where no point has road data may have success false: still a list."""
    feats = data.get("response") if isinstance(data, dict) else None
    if not isinstance(feats, list):
        err = data.get("error") if isinstance(data, dict) else None
        raise RuntimeError(f"Xweather error: {err}")
    out = {}
    for k, f in enumerate(feats):
        fid = f.get("id") or ((f.get("properties") or {}).get("request") or {}).get("id")
        out[str(k if fid is None else fid)] = f
    return out


def estimate(hourly: dict, at_s: float, rules: Rules) -> tuple[str, list[str]]:
    """Level and what (ice, fog) from Open-Meteo hourly data at at_s."""
    times = hourly.get("time") or []
    i = max((k for k, t in enumerate(times) if t <= at_s), default=None)
    if i is None:
        return "GREEN", []

    def val(name, k=i):
        vals = hourly.get(name) or []
        return vals[k] if k < len(vals) else None

    air, dew, vis = val("temperature_2m"), val("dew_point_2m"), val("visibility")
    rain = [val("precipitation", k) for k in range(max(0, i - rules.precip_hours + 1), i + 1)]
    wet = sum(r for r in rain if r) > 0
    level, what = 0, []
    if air is not None:
        if air <= rules.ice_red_c and wet:
            level, what = 2, ["ice"]
        elif air <= rules.ice_yellow_c and (
            wet or (dew is not None and air - dew <= rules.dew_gap_c)
        ):
            level, what = 1, ["ice"]
    if vis is not None and vis < rules.fog_yellow_m:
        level = max(level, 2 if vis < rules.fog_red_m else 1)
        what.append("fog")
    return LEVELS[level], what


def _meteo(client: Client, pts: list, rules: Rules) -> list[dict]:
    out = []
    for b in range(0, len(pts), METEO_BATCH):
        part = pts[b : b + METEO_BATCH]
        q = urllib.parse.urlencode(
            {
                "latitude": ",".join(f"{p[1]:.4f}" for p in part),
                "longitude": ",".join(f"{p[2]:.4f}" for p in part),
                "hourly": "temperature_2m,dew_point_2m,precipitation,visibility",
                "past_days": 1,
                "forecast_days": 2,
                "timeformat": "unixtime",
                "timezone": "GMT",
            }
        )
        _h, data = client.get(f"{OPEN_METEO}?{q}", None, "open-meteo", len(part), 0)
        out += data if isinstance(data, list) else [data]
    return out


def assess(
    pts: list,
    times: list[float],
    now_ms: int,
    client: Client,
    reserve: int = 1000,
    quota_left: int | None = None,
    rules: Rules = Rules(),
) -> dict:
    """Road weather at points (km, lat, lon) at times (ms): Xweather summary
    first, detail only where not GREEN, the Open-Meteo estimate where there
    is no road data. Below reserve no call is made. Returns per point the
    level index (None: unknown), what and source, with quota_left and
    skipped."""
    n = len(pts)
    out = {
        "levels": [None] * n,
        "what": [[] for _ in range(n)],
        "source": ["none"] * n,
        "quota_left": quota_left,
        "skipped": None,
    }
    if quota_left is not None and quota_left - n < reserve:
        out["skipped"] = "quota"
        return out
    headers, data = client.xweather("", _road_body(pts, times, now_ms))
    out["quota_left"] = left = _quota(headers)
    summary = _by_id(data)
    levels: list[int | None] = []
    for i, t in enumerate(times):
        p = _period(summary.get(str(i), {}), t / 1000)
        levels.append(None if p is None else int(p.get("summaryIndex", 0)))
    what = out["what"]
    bad = [i for i, lv in enumerate(levels) if lv]
    if bad and (left is None or left - DETAIL_COST * len(bad) >= reserve):
        headers, data = client.xweather(
            "/conditions",
            _road_body([pts[i] for i in bad], [times[i] for i in bad], now_ms),
            DETAIL_COST,
        )
        out["quota_left"] = _quota(headers)
        detail = _by_id(data)
        for k, i in enumerate(bad):
            p = _period(detail.get(str(k), {}), times[i] / 1000)
            cond = ((p or {}).get("roadSurface") or {}).get("condition")
            if cond:
                what[i] = [cond.lower()]
    elif bad:
        out["skipped"] = "detail: quota"
    source = ["road" if lv is not None else "estimate" for lv in levels]
    none = [i for i, lv in enumerate(levels) if lv is None]
    if none:
        try:
            answers = _meteo(client, [pts[i] for i in none], rules)
            if len(answers) != len(none):
                raise RuntimeError(f"Open-Meteo: {len(answers)} answers for {len(none)} points")
        except Exception as e:  # keep the road data already paid for
            print(f"ev-journal: road check estimate failed: {e!r}", file=sys.stderr)
            out["skipped"] = "; ".join(r for r in (out["skipped"], "estimate") if r)
            for i in none:
                source[i] = "none"
        else:
            for i, hourly in zip(none, answers, strict=True):
                lv, what[i] = estimate(hourly.get("hourly") or {}, times[i] / 1000, rules)
                levels[i] = LEVELS.index(lv)
    out["levels"], out["source"] = levels, source
    return out


def check(
    coords: list,
    start_ms: int,
    now_ms: int,
    client: Client,
    tz,
    kmh: float = 50.0,
    reserve: int = 1000,
    quota_left: int | None = None,
    rules: Rules = Rules(),
) -> dict:
    """Road weather along a usual way (GeoJSON coordinates) for a start at
    start_ms, at kmh. quota_left is the last known Xweather quota; below
    reserve the check is skipped. Returns the result with the new
    quota_left."""
    pts = points_along(coords)
    times = [start_ms + km / kmh * 3_600_000 for km, _a, _b in pts]
    a = assess(pts, times, now_ms, client, reserve, quota_left, rules)
    levels, source = a["levels"], a["source"]
    out = {
        "checked_at": _iso(now_ms, tz),
        "for": _iso(start_ms, tz),
        "level": None,
        "points": len(pts),
        "road_points": source.count("road"),
        "estimate_points": source.count("estimate"),
        "items": [],
        "skipped": a["skipped"],
        "quota_left": a["quota_left"],
    }
    for i, lv in enumerate(levels):
        if lv:
            out["items"].append(
                {
                    "km": round(pts[i][0], 1),
                    "level": LEVELS[lv],
                    "what": a["what"][i],
                    "at": _iso(times[i], tz),
                    "source": source[i],
                }
            )
    known = [lv for lv in levels if lv is not None]
    out["level"] = LEVELS[max(known)] if known else None  # nothing known: not GREEN
    return out


def at_points(
    points: list[tuple[float, float, float]],
    now_ms: int,
    client: Client,
    tz,
    reserve: int = 1000,
    quota_left: int | None = None,
    rules: Rules = Rules(),
) -> dict:
    """The road check for points the car sends: (lat, lon, at ms). One
    answer per point, in the order sent (#70)."""
    pts = [(0.0, lat, lon) for lat, lon, _t in points]
    times = [float(t) for _a, _b, t in points]
    a = assess(pts, times, now_ms, client, reserve, quota_left, rules)
    return {
        "points": [
            {
                "level": None if lv is None else LEVELS[lv],
                "what": what,
                "at": _iso(t, tz),
                "source": src,
            }
            for lv, what, t, src in zip(a["levels"], a["what"], times, a["source"], strict=True)
        ],
        "quota_left": a["quota_left"],
        "skipped": a["skipped"],
    }


def _iso(ms: float, tz) -> str:
    return datetime.fromtimestamp(ms / 1000, tz).isoformat(timespec="minutes")


def urllib_fetch(
    url: str, body: list | dict | None = None, headers: dict | None = None, timeout: float = 30
) -> tuple[dict, object]:
    """POST JSON when body is given, else GET. Returns (headers, JSON)."""
    import urllib.request

    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method="GET" if body is None else "POST")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return dict(r.headers.items()), json.loads(r.read())
