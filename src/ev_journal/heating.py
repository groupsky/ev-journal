"""Preheat sessions on the plug, their kWh and €, and preheat vs no preheat.

Pure functions over plain data. Rules: docs/design.md, "Heating".
"""

import bisect
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from ev_journal.forecast import night_zone
from ev_journal.homecharge import blocks

MIN = 60_000
JOIN_MS = 5 * MIN  # preheat frames with a gap under this are one session
PRICE_DAYS = 30  # the home price is learned from the home charges of these days
MIN_N = 3  # a band group with fewer journeys shows only the count
BAND_C = 5  # outside temperature band width, °C


@dataclass
class Preheat:
    start: int
    end: int
    source: str  # "state" (the car app said plugged-preheat) or "meter" (the fallback)
    wall_kwh: float | None = None
    battery_kwh: float | None = None
    outdoor_c: float | None = None
    zone: str = ""  # "day" or "night" at the start
    journey: int | None = None  # the id (start) of the next journey
    in_charge: int | None = None  # the start of the home charge it is inside of
    cost_eur: float | None = None


def from_states(frames: list[tuple[int, str]], join_ms: int = JOIN_MS) -> list[Preheat]:
    """Sessions from (time, state) car frames: runs of plugged-preheat, joined
    when the gap is under join_ms."""
    out: list[Preheat] = []
    for t, state in sorted(frames):
        if state != "plugged-preheat":
            continue
        if out and t - out[-1].end < join_ms:
            out[-1].end = t
        else:
            out.append(Preheat(start=t, end=t, source="state"))
    return out


def from_meter(
    minutes: list[tuple[int, float | None, float | None]],
    charge_ends: list[int],
    trip_starts: list[int],
    soc: list[tuple[int, float]],
    known: list[Preheat],
) -> list[Preheat]:
    """The fallback: meter power above 150 W after a charge has ended and
    before the next trip, with no SoC rise, and not inside a known preheat."""
    charge_ends, trip_starts, soc = sorted(charge_ends), sorted(trip_starts), sorted(soc)
    soc_t = [t for t, _v in soc]
    out = []
    for b in blocks(minutes):
        i = bisect.bisect_right(charge_ends, b.start)
        if not i:
            continue
        k = bisect.bisect_right(trip_starts, charge_ends[i - 1])
        if k < len(trip_starts) and trip_starts[k] <= b.end:
            continue  # a trip started between the charge end and this block
        if any(p.start <= b.end + MIN and b.start <= p.end for p in known):
            continue
        lo, hi = bisect.bisect_left(soc_t, b.start), bisect.bisect_right(soc_t, b.end + MIN)
        before = soc[lo - 1][1] if lo else None
        inside = [v for _t, v in soc[lo:hi]]
        if before is not None and inside and max(inside) > before:
            continue  # the SoC rose: a charge, not a preheat
        out.append(Preheat(start=b.start, end=b.end, source="meter"))
    return out


def _at(rows: list[tuple], t: int, col: int):
    """The last value of column col at or before t, else None."""
    v = None
    for r in rows:
        if r[0] > t:
            break
        if r[col] is not None:
            v = r[col]
    return v


def wall_kwh(minutes: list[tuple[int, float | None, float | None]], p: Preheat) -> float | None:
    """Meter total at the end minus the total before the start."""
    rows = sorted(minutes)
    a, b = _at(rows, p.start - MIN, 2), _at(rows, p.end, 2)
    return max(b - a, 0.0) if a is not None and b is not None else None


def battery_kwh(counters: list[tuple[int, float | None, float | None]], p: Preheat) -> float | None:
    """What the pack gave: Δ cum_out − Δ cum_in over the preheat."""
    rows = sorted(counters)
    o0, o1 = _at(rows, p.start, 1), _at(rows, p.end, 1)
    i0, i1 = _at(rows, p.start, 2), _at(rows, p.end, 2)
    if None in (o0, o1, i0, i1):
        return None
    return (o1 - o0) - (i1 - i0)


def zone(t: int, tz: ZoneInfo) -> str:
    """The tariff zone at t: "night" or "day"."""
    d = datetime.fromtimestamp(t / 1000, tz)
    for evening in (d.date(), d.date() - timedelta(days=1)):
        a, b = night_zone(evening, tz)
        if a <= d < b:
            return "night"
    return "day"


def home_prices(rows: list[dict], now: int) -> dict[str, float]:
    """€ per wall kWh by zone, from the home charges of the last 30 days:
    sum(cost_<zone>_eur) ÷ sum(meter_kwh_<zone>)."""
    out = {}
    recent = [r for r in rows if r["time"] >= now - PRICE_DAYS * 86_400_000]
    for z in ("day", "night"):
        pairs = [
            (r.get(f"cost_{z}_eur"), r.get(f"meter_kwh_{z}"))
            for r in recent
            if r.get(f"cost_{z}_eur") is not None and r.get(f"meter_kwh_{z}")
        ]
        kwh = sum(k for _e, k in pairs)
        if kwh > 0:
            out[z] = sum(e for e, _k in pairs) / kwh
    return out


def link(ps: list[Preheat], journey_starts: list[int]) -> None:
    """Each preheat belongs to the first journey that starts after it."""
    js = sorted(journey_starts)
    for p in ps:
        i = bisect.bisect_left(js, p.end)
        p.journey = js[i] if i < len(js) else None


def inside_charge(ps: list[Preheat], home_charges: list[tuple[int, int]]) -> None:
    """Mark a preheat that lies inside a home charge (start, end): its wall kWh
    are in that charge's meter kWh and cost already."""
    for p in ps:
        p.in_charge = next((s for s, e in home_charges if s <= p.start and p.end <= e), None)


def cost(p: Preheat, prices: dict[str, float], pool_price: float | None) -> float | None:
    """Wall kWh × the home price of its zone + battery kWh × the pool price."""
    if p.wall_kwh is None or p.zone not in prices:
        return None
    eur = p.wall_kwh * prices[p.zone]
    if p.battery_kwh and p.battery_kwh > 0:
        if pool_price is None:
            return None
        eur += p.battery_kwh * pool_price
    return eur


def charge_costs(costs: dict[int, float], ps: list[Preheat], prices: dict[str, float]) -> None:
    """Take the wall € of the preheats inside a home charge out of that
    charge's cost, so the money is not counted twice: once as charging (in
    the driving price) and once as heating."""
    for p in ps:
        if p.in_charge in costs and p.wall_kwh is not None and p.zone in prices:
            costs[p.in_charge] = max(costs[p.in_charge] - p.wall_kwh * prices[p.zone], 0.0)


def totals(r, ps: list[Preheat], tz: ZoneInfo) -> None:
    """Keep the preheats on r, and sum their wall kWh and € on the journey
    they belong to and on the month they start in."""
    from ev_journal.journeys import month_key

    r.preheats = sorted(ps, key=lambda p: p.start)
    journeys = {j.id: j for j in r.journeys}
    months = {m.key: m for m in r.months}
    for p in r.preheats:
        targets = [journeys.get(p.journey), months.get(month_key(p.start, tz)[0])]
        for x in targets:
            if x is None:
                continue
            x.preheat_kwh += p.wall_kwh or 0.0
            x.preheat_eur += p.cost_eur or 0.0


def journey_rows(r, outdoor: list[tuple[int, float]]) -> list[dict]:
    """One row per commute journey for compare(): outdoor_c at its start
    (the last reading in the hour before), and its preheat and battery numbers."""
    ts = [t for t, _c in outdoor]
    with_pre = {p.journey for p in r.preheats}
    out = []
    for j in r.journeys:
        if j.label != "commute":
            continue
        i = bisect.bisect_right(ts, j.start)
        c = outdoor[i - 1][1] if i and j.start - ts[i - 1] <= 3_600_000 else None
        pre = j.id in with_pre
        out.append(
            {
                "start": j.start,
                "outdoor_c": c,
                "preheat": pre,
                "preheat_kwh": j.preheat_kwh,
                "battery_kwh": j.kwh_used,
                "eur": None if j.cost_eur is None else j.cost_eur + j.preheat_eur,
                "km": j.km,
            }
        )
    return out


def band(c: float) -> int:
    """The lower edge of the 5 °C band of c."""
    return int(c // BAND_C * BAND_C)


def compare(rows: list[dict]) -> list[dict]:
    """Commute journeys by outside temperature band and by group.

    rows: one per commute journey: outdoor_c, preheat (bool), preheat_kwh,
    battery_kwh, eur (preheat + journey, or None), km.
    """
    by: dict[tuple[int, bool], list[dict]] = {}
    for r in rows:
        if r.get("outdoor_c") is None:
            continue
        by.setdefault((band(r["outdoor_c"]), bool(r["preheat"])), []).append(r)
    out = []
    for (lo, pre), rs in sorted(by.items()):
        g = {"band_c": [lo, lo + BAND_C], "preheat": pre, "count": len(rs)}
        if len(rs) >= MIN_N:
            eur = [r["eur"] for r in rs if r.get("eur") is not None]
            km = sum(r["km"] or 0 for r in rs)
            g |= {
                "preheat_kwh": sum(r["preheat_kwh"] or 0 for r in rs) / len(rs),
                "battery_kwh": sum(r["battery_kwh"] or 0 for r in rs) / len(rs),
                "eur": sum(eur) / len(eur) if eur else None,
                "kwh_per_100km": sum(r["battery_kwh"] or 0 for r in rs) / km * 100 if km else None,
            }
        out.append(g)
    return out


HEATER_MARGIN_C = 2.0  # a heater reading this far above the warmest module: the heater runs


def heater_on(
    heater: list[tuple[int, float | None, float | None]],
    modules: list[tuple[int, float | None]],
    margin: float = HEATER_MARGIN_C,
) -> list[int]:
    """Buckets where heater_1 or heater_2 is above the warmest module (#47).
    heater: (t, heater_1, heater_2); modules: (t, temp_max), same buckets."""
    warm = dict(modules)
    out = []
    for t, h1, h2 in heater:
        hs = [h for h in (h1, h2) if h is not None]
        if hs and warm.get(t) is not None and max(hs) > warm[t] + margin:
            out.append(t)
    return out


def on_minutes(on: list[int], spans: list[tuple[int, int]], bucket_ms: int) -> list[float]:
    """Minutes of heater-on buckets inside each span (start, end)."""
    return [sum(bucket_ms for t in on if a <= t <= b) / MIN for a, b in spans]
