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
# A fix is used for the position at a trip end only when it is this close in time.
MAX_FIX_AGE_MS = 10 * MIN


@dataclass
class Trip:
    start: int
    end: int
    kwh: float | None = None  # net battery kWh used
    km_homy: float | None = None
    estimated: bool = False  # homy filled the start (start_check = filled)
    waiting_sec: float | None = None
    waiting_kwh: float | None = None
    # filled by price.apply()
    cost_eur: float | None = None
    price_eur_per_kwh: float | None = None
    unknown_share: float | None = None
    price_estimated: bool = False
    # filled by build()
    km: float | None = None
    km_source: str = ""
    start_place: Place | None = None
    end_place: Place | None = None
    end_pos: tuple[float, float] | None = None
    fixes: list = field(default_factory=list, repr=False)


@dataclass
class Charge:
    start: int
    end: int
    kwh: float | None = None  # kWh into the battery
    type: str = "unknown"  # AC, DC or unknown (homy charge_type)
    soc_start: float | None = None
    soc_end: float | None = None
    place: Place | None = None
    # filled by price.apply()
    cost_eur: float | None = None  # money paid, when known
    home_cost: bool = False  # the cost comes from the house meter (ioniq_charges)


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
    label_by_hand: bool = False
    not_me: bool = False  # the forecast does not learn from it

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

    @property
    def cost_eur(self) -> float | None:
        """The sum of the trip costs; None when a trip has no price."""
        costs = [t.cost_eur for t in self.trips]
        return None if None in costs else sum(costs)

    @property
    def price_estimated(self) -> bool:
        return any(t.price_estimated for t in self.trips)


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
    kwh_unknown: float = 0.0  # charges whose place is not known
    journeys: int = 0
    outings: int = 0
    commute_km: float = 0.0
    odo_km: float | None = None
    # filled by price.apply()
    spent_eur: float = 0.0  # charges paid (known costs)
    driving_cost_eur: float = 0.0  # the priced trips
    priced_km: float = 0.0
    priced_kwh: float = 0.0
    drain_kwh: float = 0.0  # lost while parked, seen at each charge
    paid: dict = field(default_factory=dict)  # "home"/"away" -> [kWh, EUR] of charges with a cost
    # filled by homecharge.drift()
    kwh_per_pct: float | None = None
    meter_ratio: float | None = None
    overhead_kwh: float | None = None
    drift_flag: str = ""

    @property
    def eur_per_100km(self) -> float | None:
        return self.driving_cost_eur / self.priced_km * 100 if self.priced_km else None

    @property
    def kwh_per_100km(self) -> float | None:
        return self.kwh_used / self.km * 100 if self.km else None

    def eur_per_kwh(self, where: str | None = None) -> float | None:
        """Money paid ÷ kWh into the battery, over the charges with a known cost."""
        ps = [v for k, v in self.paid.items() if where in (None, k)]
        kwh = sum(p[0] for p in ps)
        return sum(p[1] for p in ps) / kwh if kwh else None

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
    charges: list[Charge] = field(default_factory=list)
    track: "Track | None" = None
    home_charge: dict | None = None  # the live estimate (homecharge.live)
    backtest: list[float] = field(default_factory=list)  # end SoC errors, %
    soc_model: "object | None" = None  # homecharge.Model of all home charges
    levels: list | None = None  # forecast.levels(): battery kW per AC level (#36)
    forecasts: list = field(default_factory=list)  # forecast.Forecast, scored days
    forecast_tries: list = field(default_factory=list)  # forecast.pick rows
    forecast_effects: dict = field(default_factory=dict)  # forecast.plan: keys, rows, table
    advice: dict | None = None  # tomorrow's forecast and charging advice
    advice_ctx: dict | None = None  # forecast.plan: what forecast.on_request needs


class Track:
    """Real GPS fixes (t, lat, lon) in time order."""

    def __init__(self, fixes: list):
        self.fixes = sorted(fixes)
        self.times = [f[0] for f in self.fixes]

    def between(self, start: int, end: int) -> list:
        i, k = bisect.bisect_left(self.times, start), bisect.bisect_right(self.times, end)
        return self.fixes[i:k]


def _journey_at(journeys: list[Journey], t: int) -> Journey | None:
    starts = [j.start for j in journeys]
    i = bisect.bisect_right(starts, t)
    if i and t <= journeys[i - 1].end:
        return journeys[i - 1]
    return None


def _overlap(a0: int, a1: int, b0: int, b1: int) -> int:
    return max(0, min(a1, b1) - max(a0, b0))


def _is_workday(t: int, tz: ZoneInfo) -> bool:
    return datetime.fromtimestamp(t / 1000, tz).weekday() < 5


def _label(j: Journey, tz: ZoneInfo) -> str:
    kinds = {p.kind if p else None for p in (j.start_place, j.end_place)}
    return "commute" if kinds == {"home", "work"} and _is_workday(j.start, tz) else "other"


def month_key(t: int, tz: ZoneInfo) -> tuple[str, int]:
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
    labels: list | None = None,
) -> Result:
    """Build everything from the sessions, the real GPS fixes and the places.

    odometer is a list of (t, km) readings, for the monthly check.
    labels is a list of (t, label or None, not_me) set by hand, oldest first.
    A label attaches to the journey that contains its time.
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
        # Only fixes of the trip itself, near its start and end. An old fix could
        # be from another place.
        first, last = (t.fixes[0], t.fixes[-1]) if t.fixes else (None, None)
        if first and first[0] - t.start <= MAX_FIX_AGE_MS:
            t.start_place = place_at(places, (first[1], first[2]))
        if last and t.end - last[0] <= MAX_FIX_AGE_MS:
            t.end_pos = (last[1], last[2])
            t.end_place = place_at(places, t.end_pos)
    # A charge is at the place where the last trip before it ended.
    ends = [t.end for t in trips]
    for c in charges:
        i = bisect.bisect_right(ends, c.start)
        c.place = trips[i - 1].end_place if i else None

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
    # The newest label row inside a journey wins. A label of None means
    # "use the computed one".
    newest = {}
    for t, label, not_me in labels or []:
        j = _journey_at(journeys, t)
        if j is not None:
            newest[j.id] = (j, label, not_me)
    for j, label, not_me in newest.values():
        if label is not None:
            j.label, j.label_by_hand = label, True
        j.not_me = not_me

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
        key, start = month_key(t, tz)
        return months.setdefault(key, Month(start=start, key=key))

    for t in trips:
        m = month(t.start)
        m.trips += 1
        m.km += t.km or 0
        m.kwh_used += t.kwh or 0
    for c in charges:
        m = month(c.start)
        if c.place is None:
            m.kwh_unknown += c.kwh or 0
        elif c.place.kind == "home":
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
    return Result(
        trips=trips, journeys=journeys, outings=outings, months=ms, charges=charges, track=track
    )


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
