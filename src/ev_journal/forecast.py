"""Forecast of tomorrow's energy need, and charging advice (#9).

Rules and the numbers behind them: docs/forecast.md.
"""

import math
import statistics
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from ev_journal.homecharge import quantile
from ev_journal.journeys import Result

POOL_DAYS = 28  # learn from the days of the same kind in this many days before
MIN_POOL = 3
BAD_MARGIN_KWH = 2.0
SCORE_DAYS = 91  # about the last 3 months
STEP_MIN = 15  # the start time is set in steps of this many minutes
TARGET_STEP = 5  # the target SoC is rounded up to this many %

# The car's AC levels: (name, meter kW, battery kW), measured 2024-11 → 2026-10.
LEVELS = [("low", 1.22, 0.92), ("mid", 1.93, 1.60), ("high", 2.64, 2.34)]
LEVEL_BG = {"low": "ниско", "mid": "средно", "high": "високо"}


def day_type(d: date, holidays: set[date]) -> str:
    """workday, weekend or holiday (official holidays and extra days off)."""
    if d.weekday() >= 5:
        return "weekend"
    return "holiday" if d in holidays else "workday"


def kind(dtype: str) -> str:
    """Forecast pools: workdays, and the rest ("off")."""
    return "workday" if dtype == "workday" else "off"


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


def forecast(need: dict[date, float], day: date, holidays: set[date]) -> Forecast | None:
    """From the days of the same kind in the 28 days before day."""
    dtype = day_type(day, holidays)
    pool = [
        v
        for d, v in need.items()
        if day - timedelta(days=POOL_DAYS) <= d < day and kind(day_type(d, holidays)) == kind(dtype)
    ]
    if len(pool) < MIN_POOL:
        return None
    return Forecast(day, dtype, quantile(pool, 0.5), quantile(pool, 0.9) + BAD_MARGIN_KWH)


def day_start(d: date, tz: ZoneInfo) -> int:
    return int(datetime.combine(d, time(), tz).timestamp() * 1000)


def backtest(
    need: dict[date, float], holidays: set[date], days: int = SCORE_DAYS
) -> list[Forecast]:
    """The forecast of each of the last days, made only from the days before it."""
    if not need:
        return []
    last = max(need)
    out = []
    for i in range(days, 0, -1):
        d = last - timedelta(days=i - 1)
        f = forecast(need, d, holidays)
        if f is not None and d in need:
            f.real_kwh = need[d]
            out.append(f)
    return out


def score(fs: list[Forecast]) -> dict[str, dict]:
    """Per day type: days, share covered by the bad day, mean error of the normal day."""
    out = {}
    for dtype in ("workday", "weekend", "holiday", "all"):
        g = [f for f in fs if dtype in ("all", f.day_type)]
        if g:
            out[dtype] = {
                "days": len(g),
                "covered_pct": sum(f.bad_kwh >= f.real_kwh for f in g) / len(g) * 100,
                "normal_error_kwh": statistics.fmean(abs(f.normal_kwh - f.real_kwh) for f in g),
            }
    return out


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
) -> Advice:
    """The lowest level that reaches the target inside the night zone, ending
    as close to departure as the zone allows."""
    want = bad_kwh / kwh_per_pct + reserve_pct
    target = min(math.ceil(want / TARGET_STEP) * TARGET_STEP, 100)
    if soc_now >= target:
        return Advice(target=target)
    z0, z1 = zone
    end = departure if departure is not None and z0 < departure < z1 else z1
    window_min = (end - z0).total_seconds() / 60
    for name, _meter_kw, kw in LEVELS:
        minutes = (target - soc_now) * kwh_per_pct / kw * 60
        minutes = math.ceil(minutes / STEP_MIN) * STEP_MIN
        if minutes <= window_min:
            return Advice(target, name, end - timedelta(minutes=minutes), end)
    name, _meter_kw, kw = LEVELS[-1]
    reach = math.floor(soc_now + window_min / 60 * kw / kwh_per_pct)
    return Advice(min(reach, 100), name, z0, end, capped=True)


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
) -> dict | None:
    """Fill r.forecasts (the scored last days) and return the advice for the
    next morning: tomorrow in the evening, today after midnight."""
    local = datetime.fromtimestamp(now / 1000, tz)
    today = local.date()
    r.forecasts = backtest(daily_need(r, tz, today - timedelta(days=1)), hols)
    for f in r.forecasts:
        f.start = day_start(f.day, tz)
    day = (local + timedelta(hours=12)).date()
    f = forecast(daily_need(r, tz, min(day - timedelta(days=1), today)), day, hols)
    if f is None or soc_now is None:
        return None
    dep = None
    if departure and f.day_type == "workday":
        dep = datetime.combine(day, time.fromisoformat(departure), tz)
    a = advise(
        soc_now, f.bad_kwh, kwh_per_pct, night_zone(day - timedelta(days=1), tz), dep, reserve_pct
    )
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
        "capped": a.capped,
        "preheat_off": preheat_off,
        "message": message(f, soc_now, a, preheat_off),
        "score": score(r.forecasts),
    }
