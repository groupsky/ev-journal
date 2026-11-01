"""The price of trips, journeys and months: one average pool in battery kWh.

Rules: docs/design.md ("Price of a trip") and docs/price.md.
"""

import statistics
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from ev_journal.journeys import Charge, Result

DEFAULT_KWH_PER_PCT = 0.30
MIN_SOC_STEP = 20.0  # learn kWh per % only from charges of at least this many %
MAX_UNKNOWN_SHARE = 0.10


def kwh_per_pct(charges: list[Charge], home: set[int]) -> float:
    """Battery kWh per SoC %, learned from home charges."""
    vals = [
        c.kwh / (c.soc_end - c.soc_start)
        for c in charges
        if c.start in home
        and c.kwh
        and c.soc_start is not None
        and c.soc_end is not None
        and c.soc_end - c.soc_start >= MIN_SOC_STEP
    ]
    return statistics.median(vals) if vals else DEFAULT_KWH_PER_PCT


@dataclass
class Pool:
    known_kwh: float = 0.0
    known_eur: float = 0.0
    unknown_kwh: float = 0.0

    @property
    def kwh(self) -> float:
        return self.known_kwh + self.unknown_kwh

    @property
    def unknown_share(self) -> float:
        return self.unknown_kwh / self.kwh if self.kwh > 0 else 1.0

    @property
    def price(self) -> float | None:
        return self.known_eur / self.known_kwh if self.known_kwh > 0 else None

    def scale_to(self, kwh: float) -> float:
        """Set the pool to kwh, keeping the shares and the price. Returns the drop."""
        drop = self.kwh - kwh
        if self.kwh > 0:
            f = max(kwh, 0.0) / self.kwh
            self.known_kwh *= f
            self.known_eur *= f
            self.unknown_kwh *= f
        else:
            self.unknown_kwh = max(kwh, 0.0)
        return drop

    def take(self, kwh: float) -> None:
        if self.kwh <= 0:
            return
        self.scale_to(max(self.kwh - kwh, 0.0))


def apply(r: Result, costs: dict[int, float], home: set[int], tz: ZoneInfo) -> float:
    """Price every trip, journey and month in r from the whole history.

    costs: the money paid per charge start (home and away, when known).
    home: the starts of the home charges, to learn kWh per %.
    Returns the kWh per % used.
    """
    k = kwh_per_pct(r.charges, home)
    events = [(c.start, 0, c) for c in r.charges] + [(t.start, 1, t) for t in r.trips]
    pool = Pool()
    months = {m.key: m for m in r.months}

    def month(t: int):
        d = datetime.fromtimestamp(t / 1000, tz)
        return months.get(f"{d.year:04d}-{d.month:02d}")

    for _t, kind, e in sorted(events, key=lambda x: (x[0], x[1])):
        if kind == 0:  # a charge
            c = e
            if c.soc_start is not None:
                drop = pool.scale_to(c.soc_start * k)
                if (m := month(c.start)) and pool.kwh > 0 and drop > 0:
                    m.drain_kwh += drop
            c.cost_eur = costs.get(c.start)
            c.home_cost = c.start in home
            if not c.kwh:
                continue
            if c.cost_eur is not None:
                pool.known_kwh += c.kwh
                pool.known_eur += c.cost_eur
                if m := month(c.start):
                    m.spent_eur += c.cost_eur
            else:
                pool.unknown_kwh += c.kwh
        else:  # a trip
            t = e
            share = pool.unknown_share
            t.unknown_share = share
            if t.kwh and pool.price is not None and share <= MAX_UNKNOWN_SHARE:
                t.price_eur_per_kwh = pool.price
                t.cost_eur = t.kwh * pool.price
                t.price_estimated = share > 0
                if m := month(t.start):
                    m.driving_cost_eur += t.cost_eur
                    m.priced_km += t.km or 0
                    m.priced_kwh += t.kwh
            if t.kwh:
                pool.take(t.kwh)
    return k
