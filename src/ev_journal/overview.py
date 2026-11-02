"""Cost per kWh and per 100 km over all time and the last 30 days.

Months get the same numbers from price.apply().

Rules: docs/price.md ("Overview").
"""

from ev_journal.journeys import Charge, Result, Trip

DAY_MS = 86_400_000


def _div(a: float, b: float, k: float = 1.0) -> float | None:
    return a / b * k if b else None


def stats(charges: list[Charge], trips: list[Trip]) -> dict:
    """The numbers of one period. None where the period has no data for it.

    Price per kWh: money paid ÷ kWh into the battery, over the charges with a
    known cost. Per 100 km: the priced trips (the pool price, docs/price.md).
    kWh per 100 km: all trips with km and kWh.
    """
    paid = [c for c in charges if c.kwh and c.cost_eur is not None]
    home = [c for c in paid if c.home_cost]
    away = [c for c in paid if not c.home_cost]
    priced = [t for t in trips if t.cost_eur is not None and t.km]
    used = [t for t in trips if t.kwh is not None and t.km]

    def per_kwh(cs: list[Charge]) -> float | None:
        return _div(sum(c.cost_eur for c in cs), sum(c.kwh for c in cs))

    return {
        "eur_per_kwh": per_kwh(paid),
        "eur_per_kwh_home": per_kwh(home),
        "eur_per_kwh_away": per_kwh(away),
        "eur_per_100km": _div(sum(t.cost_eur for t in priced), sum(t.km for t in priced), 100),
        "kwh_per_100km": _div(sum(t.kwh for t in used), sum(t.km for t in used), 100),
        "charges_no_cost": sum(1 for c in charges if c.kwh and c.cost_eur is None),
        "trips_no_price": len(trips) - len(priced),
    }


def overview(r: Result, now: int) -> dict:
    """All time and the last 30 days before now."""
    lo = now - 30 * DAY_MS
    return {
        "all": stats(r.charges, r.trips),
        "last_30d": stats(
            [c for c in r.charges if lo <= c.start < now],
            [t for t in r.trips if lo <= t.start < now],
        ),
    }
