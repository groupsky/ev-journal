"""Trips, stops, journeys, outings and months. Pure functions over plain data.

Times are epoch milliseconds. Rules: docs/design.md, "Four levels".
"""

import bisect
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

from ev_journal.geo import track_km
from ev_journal.places import Place, place_at

MIN = 60_000
SPLIT_NOT_CHARGING_MS = 30 * MIN


@dataclass
class Trip:
    start: int
    end: int
    kwh: float | None = None  # net battery kWh used
    km_homy: float | None = None
    estimated: bool = False  # homy filled the start (start_check = filled)
    waiting_sec: float | None = None
    waiting_kwh: float | None = None
    # filled by build()
    km: float | None = None
    km_source: str = ""
    start_place: Place | None = None
    end_place: Place | None = None
    fixes: list = field(default_factory=list, repr=False)


@dataclass
class Charge:
    start: int
    end: int
    kwh: float | None = None  # kWh into the battery
    place: Place | None = None


@dataclass
class Stop:
    start: int
    end: int
    place: Place | None
    charges: list[Charge]
    charge_ms: int

    @property
    def not_charging_ms(self) -> int:
        return self.end - self.start - self.charge_ms

    @property
    def splits(self) -> bool:
        if self.not_charging_ms > SPLIT_NOT_CHARGING_MS:
            return True
        # A home charge always ends the journey and the outing.
        return self.place is not None and self.place.kind == "home" and self.charge_ms > 0


@dataclass
class Journey:
    trips: list[Trip]
    stops: list[Stop]  # the stops inside the journey (joined)
    label: str = "other"

    @property
    def id(self) -> int:
        return self.trips[0].start

    @property
    def start(self) -> int:
        return self.trips[0].start

    @property
    def end(self) -> int:
        return self.trips[-1].end

    @property
    def start_place(self) -> Place | None:
        return self.trips[0].start_place

    @property
    def end_place(self) -> Place | None:
        return self.trips[-1].end_place

    @property
    def km(self) -> float:
        return sum(t.km or 0 for t in self.trips)

    @property
    def kwh_used(self) -> float:
        return sum(t.kwh or 0 for t in self.trips)

    @property
    def drive_min(self) -> float:
        return sum(t.end - t.start for t in self.trips) / MIN

    @property
    def waiting_min(self) -> float:
        return sum(s.not_charging_ms for s in self.stops) / MIN

    @property
    def charge_min(self) -> float:
        return sum(s.charge_ms for s in self.stops) / MIN

    @property
    def charges(self) -> list[Charge]:
        return [c for s in self.stops for c in s.charges]

    @property
    def kwh_charged(self) -> float:
        return sum(c.kwh or 0 for c in self.charges)


@dataclass
class Outing:
    journeys: list[Journey]

    @property
    def start(self) -> int:
        return self.journeys[0].start

    @property
    def end(self) -> int:
        return self.journeys[-1].end

    @property
    def km(self) -> float:
        return sum(j.km for j in self.journeys)

    @property
    def kwh(self) -> float:
        return sum(j.kwh_used for j in self.journeys)


@dataclass
class Month:
    start: int  # first ms of the month, local time
    key: str  # "2026-01"
    trips: int = 0
    km: float = 0.0
    kwh_used: float = 0.0
    kwh_home: float = 0.0
    kwh_away: float = 0.0
    journeys: int = 0
    outings: int = 0
    commute_km: float = 0.0
    odo_km: float | None = None

    @property
    def gap_pct(self) -> float | None:
        if not self.odo_km:
            return None
        return (self.km - self.odo_km) / self.odo_km * 100


@dataclass
class Result:
    trips: list[Trip]
    journeys: list[Journey]
    outings: list[Outing]
    months: list[Month]


class Track:
    """Real GPS fixes (t, lat, lon) in time order."""

    def __init__(self, fixes: list):
        self.fixes = sorted(fixes)
        self.times = [f[0] for f in self.fixes]

    def between(self, start: int, end: int) -> list:
        i, k = bisect.bisect_left(self.times, start), bisect.bisect_right(self.times, end)
        return self.fixes[i:k]

    def position_at(self, t: int) -> tuple[float, float] | None:
        """The last fix at or before t."""
        i = bisect.bisect_right(self.times, t)
        if i == 0:
            return None
        return self.fixes[i - 1][1], self.fixes[i - 1][2]


def _overlap(a0: int, a1: int, b0: int, b1: int) -> int:
    return max(0, min(a1, b1) - max(a0, b0))


def _is_workday(t: int, tz: ZoneInfo) -> bool:
    return datetime.fromtimestamp(t / 1000, tz).weekday() < 5


def _label(j: Journey, tz: ZoneInfo) -> str:
    kinds = {p.kind if p else None for p in (j.start_place, j.end_place)}
    return "commute" if kinds == {"home", "work"} and _is_workday(j.start, tz) else "other"


def _month_key(t: int, tz: ZoneInfo) -> tuple[str, int]:
    d = datetime.fromtimestamp(t / 1000, tz)
    first = d.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return f"{d.year:04d}-{d.month:02d}", int(first.timestamp() * 1000)


def build(
    trips: list[Trip],
    charges: list[Charge],
    fixes: list,
    places: list[Place],
    tz: ZoneInfo,
    odometer: list | None = None,
) -> Result:
    """Build everything from the sessions, the real GPS fixes and the places.

    odometer is a list of (t, km) readings, for the monthly check.
    """
    track = Track(fixes)
    trips = sorted(trips, key=lambda t: t.start)
    charges = sorted(charges, key=lambda c: c.start)

    for t in trips:
        t.fixes = track.between(t.start, t.end)
        if len(t.fixes) >= 2:
            t.km, t.km_source = track_km(t.fixes), "gps"
        else:
            t.km, t.km_source = t.km_homy, "homy"
        start_pos = (t.fixes[0][1], t.fixes[0][2]) if t.fixes else track.position_at(t.start)
        t.start_place = place_at(places, start_pos)
        t.end_place = place_at(places, track.position_at(t.end))
    for c in charges:
        c.place = place_at(places, track.position_at(c.start))

    # Journeys: split the trips at the stops that split.
    journeys: list[Journey] = []
    cur = Journey(trips=[], stops=[])
    for i, t in enumerate(trips):
        cur.trips.append(t)
        if i + 1 == len(trips):
            break
        s0, s1 = t.end, trips[i + 1].start
        in_stop = [c for c in charges if _overlap(c.start, c.end, s0, s1) > 0]
        stop = Stop(
            start=s0,
            end=s1,
            place=t.end_place,
            charges=in_stop,
            charge_ms=sum(_overlap(c.start, c.end, s0, s1) for c in in_stop),
        )
        if stop.splits:
            journeys.append(cur)
            cur = Journey(trips=[], stops=[])
        else:
            cur.stops.append(stop)
    if cur.trips:
        journeys.append(cur)
    for j in journeys:
        j.label = _label(j, tz)

    # Outings: from home back to home. An outing not yet home is left out.
    outings: list[Outing] = []
    pending: list[Journey] = []
    for j in journeys:
        pending.append(j)
        if j.end_place is not None and j.end_place.kind == "home":
            outings.append(Outing(journeys=pending))
            pending = []

    months: dict[str, Month] = {}

    def month(t: int) -> Month:
        key, start = _month_key(t, tz)
        return months.setdefault(key, Month(start=start, key=key))

    for t in trips:
        m = month(t.start)
        m.trips += 1
        m.km += t.km or 0
        m.kwh_used += t.kwh or 0
    for c in charges:
        m = month(c.start)
        if c.place is not None and c.place.kind == "home":
            m.kwh_home += c.kwh or 0
        else:
            m.kwh_away += c.kwh or 0
    for j in journeys:
        m = month(j.start)
        m.journeys += 1
        if j.label == "commute":
            m.commute_km += j.km
    for o in outings:
        month(o.start).outings += 1

    ms = sorted(months.values(), key=lambda m: m.start)
    if odometer:
        _odometer_check(ms, sorted(odometer), tz)
    return Result(trips=trips, journeys=journeys, outings=outings, months=ms)


def _odometer_check(months: list[Month], odo: list, tz: ZoneInfo) -> None:
    """Odometer change per month: last reading before the month ends minus the
    last reading before it starts (or the first reading in it)."""
    times = [r[0] for r in odo]
    for m in months:
        d = datetime.fromtimestamp(m.start / 1000, tz)
        nxt = d.replace(year=d.year + (d.month == 12), month=d.month % 12 + 1)
        end = int(nxt.timestamp() * 1000)
        i0 = bisect.bisect_left(times, m.start)
        i1 = bisect.bisect_left(times, end)
        if i1 == 0 or i0 == len(odo):
            continue
        start_km = odo[i0 - 1][1] if i0 > 0 else odo[i0][1]
        m.odo_km = odo[i1 - 1][1] - start_km
