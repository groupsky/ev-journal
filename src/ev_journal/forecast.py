"""Forecast of tomorrow's energy need, and charging advice (#9).

Rules and the numbers behind them: docs/forecast.md.
"""

import math
import statistics
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from ev_journal import calendar_keys as ck
from ev_journal.homecharge import quantile
from ev_journal.journeys import Result

POOL_DAYS = 28  # learn from the days of the same kind in this many days before
MIN_POOL = 3
BAD_MARGIN_KWH = 2.0
SCORE_DAYS = 91  # about the last 3 months
STEP_MIN = 15  # default step of the car's charge timer, in minutes
TARGET_STEP = 5  # the target SoC is rounded up to this many %

# The car's AC levels: (name, meter kW, battery kW), measured 2024-11 → 2026-10.
LEVELS = [("low", 1.22, 0.92), ("mid", 1.93, 1.60), ("high", 2.64, 2.34)]
LEVEL_BG = {"low": "ниско", "mid": "средно", "high": "високо"}


def day_type(d: date, holidays: set[date], ranges=()) -> str:
    """workday, weekend or holiday (official holidays and extra days off).

    ranges: (name, first, last) date ranges of extra day types. A workday
    inside a range gets that name; weekends and holidays keep theirs.
    """
    if d.weekday() >= 5:
        return "weekend"
    if d in holidays:
        return "holiday"
    return next((name for name, a, b in ranges if a <= d <= b), "workday")


def kind(dtype: str) -> str:
    """Forecast pools: workdays (extra types too), and the rest ("off")."""
    return "off" if dtype in ("weekend", "holiday") else "workday"


def daily_need(r: Result, tz: ZoneInfo, until: date) -> dict[date, float]:
    """Battery kWh of the trips that start on each local day, up to and
    including until. Journeys marked "not me" are left out. A day with no
    trips counts as 0."""
    skip = {id(t) for j in r.journeys if j.not_me for t in j.trips}
    need: dict[date, float] = {}
    for t in r.trips:
        if id(t) in skip:
            continue
        d = datetime.fromtimestamp(t.start / 1000, tz).date()
        if d <= until:
            need[d] = need.get(d, 0.0) + (t.kwh or 0.0)
    if not need:
        return {}
    d, out = min(need), {}
    while d <= until:
        out[d] = need.get(d, 0.0)
        d += timedelta(days=1)
    return out


@dataclass
class Forecast:
    day: date
    day_type: str
    normal_kwh: float  # p50
    bad_kwh: float  # p90 + margin
    real_kwh: float | None = None
    start: int = 0  # the day's first ms, local time
    choice: tuple[str, ...] = ()  # the calendar keys of the pool


def chain(choice: tuple[str, ...], dtype: str) -> list[tuple[str, ...]]:
    """The choices to try, simplest last: a pair, then its first key, then no
    key. A day of a named range tries the past days of the same name first."""
    out = [choice[:i] for i in range(len(choice), -1, -1)]
    if kind(dtype) == "workday" and dtype != "workday":
        out.insert(0, ("day_type",))
    return out


def forecast(
    need: dict[date, float], day: date, holidays: set[date], ranges=(), choices=None
) -> Forecast | None:
    """From the past days of the same kind and the same calendar keys.

    choices: the keys per kind ("workday", "off"); none: the 28 days before.
    A pool with fewer than MIN_POOL days falls back to the next simpler choice."""
    dtype = day_type(day, holidays, ranges)
    for choice in chain((choices or {}).get(kind(dtype), ()), dtype):
        first = day - timedelta(days=ck.window(choice, POOL_DAYS))
        v = ck.value(choice, day, dtype)
        pool = []
        for d, x in need.items():
            if first <= d < day:
                t = day_type(d, holidays, ranges)
                if kind(t) == kind(dtype) and ck.value(choice, d, t) == v:
                    pool.append(x)
        if len(pool) >= MIN_POOL:
            normal, bad = quantile(pool, 0.5), quantile(pool, 0.9) + BAD_MARGIN_KWH
            return Forecast(day, dtype, normal, bad, choice=choice)
    return None


def day_start(d: date, tz: ZoneInfo) -> int:
    return int(datetime.combine(d, time(), tz).timestamp() * 1000)


def backtest(
    need: dict[date, float],
    holidays: set[date],
    days: int = SCORE_DAYS,
    ranges=(),
    choices=None,
    only: str | None = None,
) -> list[Forecast]:
    """The forecast of each of the last days, made only from the days before it.
    only: a kind, to forecast only the days of that kind."""
    if not need:
        return []
    last = max(need)
    out = []
    for i in range(days, 0, -1):
        d = last - timedelta(days=i - 1)
        if only and kind(day_type(d, holidays, ranges)) != only:
            continue
        f = forecast(need, d, holidays, ranges, choices)
        if f is not None:
            f.real_kwh = need[d]
            out.append(f)
    return out


def score(fs: list[Forecast]) -> dict[str, dict]:
    """Per day type: days, share covered by the bad day, mean error of the
    normal day. The days of named ranges count as workdays."""
    out = {}
    for dtype in ("workday", "weekend", "holiday", "all"):
        g = [f for f in fs if dtype in ("all", f.day_type) or dtype == kind(f.day_type)]
        if g:
            out[dtype] = {
                "days": len(g),
                "covered_pct": sum(f.bad_kwh >= f.real_kwh for f in g) / len(g) * 100,
                "normal_error_kwh": statistics.fmean(abs(f.normal_kwh - f.real_kwh) for f in g),
            }
    return out


def beats(s: dict, base: dict) -> bool:
    """Bad-day coverage not lower, and normal-day error lower."""
    return (
        s["covered_pct"] >= base["covered_pct"]
        and s["normal_error_kwh"] < base["normal_error_kwh"] - 1e-9
    )


def pick(
    need: dict[date, float], holidays: set[date], ranges=(), days: int = SCORE_DAYS
) -> tuple[dict[str, tuple[str, ...]], list[dict]]:
    """Per kind, the simplest choice of calendar keys that beats no key in the
    back-test: the single key with the lowest error among those that beat
    it; else the pair of the two single keys with the lowest error, if it
    beats it; else no key. Returns the picks and a row for each tried choice."""
    picks, rows = {}, []
    for k in ("workday", "off"):
        base_fs = backtest(need, holidays, days, ranges, only=k)
        if not base_fs:
            continue
        base_days = {f.day for f in base_fs}

        def run(choice, k=k, base_days=base_days):
            fs = backtest(need, holidays, days, ranges, {k: choice}, only=k)
            # Only the days that no key scores, so all choices compare alike.
            fs = [f for f in fs if f.day in base_days]
            s = score(fs)["all"]
            rows.append({"kind": k, "choice": choice, **s, "picked": False})
            return rows[-1]

        base = run(())
        singles = sorted((run((key,)) for key in ck.KEYS), key=lambda r: r["normal_error_kwh"])
        best = next((r for r in singles if beats(r, base)), None)
        if best is None:
            pair = run(singles[0]["choice"] + singles[1]["choice"])
            if beats(pair, base):
                best = pair
        best = best or base
        best["picked"] = True
        picks[k] = best["choice"]
    return picks, rows


def night_zone(evening: date, tz: ZoneInfo) -> tuple[datetime, datetime]:
    """The night tariff zone that starts on the evening of a day.

    22:00–06:00 from 1 November to 31 March, 23:00–07:00 from 1 April to 31 October.
    """
    h = 22 if evening.month in (11, 12, 1, 2, 3) else 23
    start = datetime.combine(evening, time(h), tz)
    end = datetime.combine(evening + timedelta(days=1), time(h - 16), tz)
    return start, end


@dataclass
class Advice:
    target: float  # SoC % at departure
    level: str | None = None  # None: no charge needed
    start: datetime | None = None
    end: datetime | None = None
    capped: bool = False  # the night zone cannot reach the wanted target


def advise(
    soc_now: float,
    bad_kwh: float,
    kwh_per_pct: float,
    zone: tuple[datetime, datetime],
    departure: datetime | None,
    reserve_pct: float,
    now: datetime | None = None,
    step_min: int = STEP_MIN,
) -> Advice:
    """The lowest level that reaches the target inside the night zone, ending
    as close to departure as the zone allows. The charge starts no earlier
    than now. Sums are in UTC, so the nights the clocks change are right."""
    want = bad_kwh / kwh_per_pct + reserve_pct
    target = min(math.ceil(want / TARGET_STEP) * TARGET_STEP, 100)
    if soc_now >= target:
        return Advice(target=target)
    tz = zone[1].tzinfo
    z0, z1 = (z.astimezone(UTC) for z in zone)
    if now is not None:
        z0 = max(z0, now.astimezone(UTC))
    dep = departure.astimezone(UTC) if departure is not None else None
    end = dep if dep is not None and z0 < dep < z1 else z1
    window_min = max((end - z0).total_seconds() / 60, 0)
    for name, _meter_kw, kw in LEVELS:
        minutes = (target - soc_now) * kwh_per_pct / kw * 60
        minutes = math.ceil(minutes / step_min) * step_min
        if minutes <= window_min:
            start = end - timedelta(minutes=minutes)
            return Advice(target, name, start.astimezone(tz), end.astimezone(tz))
    name, _meter_kw, kw = LEVELS[-1]
    reach = math.floor(soc_now + window_min / 60 * kw / kwh_per_pct)
    return Advice(min(reach, 100), name, z0.astimezone(tz), end.astimezone(tz), capped=True)


def message(f: Forecast, soc_now: float, a: Advice, preheat_off: bool) -> str:
    """The evening message, in Bulgarian."""
    s = f"Утре: около {f.normal_kwh:.0f} kWh (лош ден {f.bad_kwh:.0f}). Сега {soc_now:.0f} %."
    if a.level is None:
        s += " Не е нужно зареждане тази вечер."
    else:
        s += (
            f" Нужни {a.target:.0f} % при тръгване."
            f" Задай: {LEVEL_BG[a.level]}, {a.start:%H:%M}–{a.end:%H:%M}."
        )
        if a.capped:
            s += " Повече не се достига в нощната зона."
    if preheat_off:
        s += " Утре е почивен ден: изключи затоплянето."
    return s


def holiday_set(country: str | None, days_off: tuple[str, ...], years: range) -> set[date]:
    """Official holidays of a country (python-holidays) plus extra days off."""
    out = {date.fromisoformat(d) for d in days_off}
    if country:
        import holidays

        out |= set(holidays.country_holidays(country, years=years))
    return out


def plan(
    r: Result,
    tz: ZoneInfo,
    now: int,
    soc_now: float | None,
    kwh_per_pct: float,
    hols: set[date],
    departure: str | None = None,
    reserve_pct: float = 10.0,
    preheat: bool = False,
    departures: dict[str, str | None] | None = None,
    ranges=(),
    step_min: int = STEP_MIN,
) -> dict | None:
    """Fill r.forecasts (the scored last days) and return the advice for the
    next morning: tomorrow in the evening, today after midnight.

    The pool takes the days up to the one before that morning, so in the
    evening today's trips count. Then the evening forecast is the same as
    the back-test's forecast for that day."""
    local = datetime.fromtimestamp(now / 1000, tz)
    today = local.date()
    past = daily_need(r, tz, today - timedelta(days=1))
    picks, r.forecast_tries = pick(past, hols, ranges)
    r.forecasts = backtest(past, hols, ranges=ranges, choices=picks)
    for f in r.forecasts:
        f.start = day_start(f.day, tz)
    day = (local + timedelta(hours=12)).date()
    need = daily_need(r, tz, min(day - timedelta(days=1), today))
    f = forecast(need, day, hols, ranges, picks)
    if f is None or soc_now is None:
        return None
    # The departure of the day type; else the default. None: the zone end.
    hhmm = (departures or {}).get(f.day_type, departure)
    dep = datetime.combine(day, time.fromisoformat(hhmm), tz) if hhmm else None
    zone = night_zone(day - timedelta(days=1), tz)
    a = advise(soc_now, f.bad_kwh, kwh_per_pct, zone, dep, reserve_pct, local, step_min)
    preheat_off = preheat and f.day_type == "holiday"
    return {
        "day": day.isoformat(),
        "day_type": f.day_type,
        "normal_kwh": round(f.normal_kwh, 1),
        "bad_kwh": round(f.bad_kwh, 1),
        "soc_now": round(soc_now, 1),
        "target": a.target,
        "level": a.level,
        "start": a.start.isoformat() if a.start else None,
        "end": a.end.isoformat() if a.end else None,
        "departure": dep.isoformat() if dep else None,
        "capped": a.capped,
        "preheat_off": preheat_off,
        "message": message(f, soc_now, a, preheat_off),
        "score": score(r.forecasts),
    }
