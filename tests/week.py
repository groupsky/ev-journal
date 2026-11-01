"""A made-up week in the test area (within 1 degree of 0 N 0 E, open sea).

Monday 2026-01-05 to Saturday 2026-01-10, UTC.
"""

from datetime import UTC, datetime

from ev_journal.journeys import Charge, Trip
from ev_journal.places import Place

HOME = Place("p-home", "Test Home", "home", 0.1, 0.1, 150)
WORK = Place("p-work", "Test Work", "work", 0.2, 0.3, 150)
STATION = Place("p-station", "Test Station", "station", 0.5, 0.5, 150)
LAKE = Place("p-lake", "Test Lake", "other", 0.7, -0.2, 150)
PLACES = [HOME, WORK, STATION, LAKE]

MIN = 60_000


def ms(day: int, hh: int, mm: int = 0) -> int:
    return int(datetime(2026, 1, day, hh, mm, tzinfo=UTC).timestamp() * 1000)


class Week:
    def __init__(self):
        self.trips: list[Trip] = []
        self.charges: list[Charge] = []
        self.fixes: list = []

    def drive(self, start: int, minutes: int, a: Place, b: Place, kwh: float = 3.0):
        """A straight drive from a to b with a fix every 10 s."""
        end = start + minutes * MIN
        n = minutes * 6
        for i in range(n + 1):
            f = i / n
            self.fixes.append(
                (start + i * 10_000, a.lat + (b.lat - a.lat) * f, a.lon + (b.lon - a.lon) * f)
            )
        self.trips.append(Trip(start=start, end=end, kwh=kwh))
        return end

    def charge(self, start: int, minutes: int, kwh: float):
        self.charges.append(Charge(start=start, end=start + minutes * MIN, kwh=kwh))


def week() -> Week:
    w = Week()
    # Mon: commute both ways. The stop at work is 9 h: it splits.
    w.drive(ms(5, 8), 30, HOME, WORK)
    w.drive(ms(5, 17, 30), 30, WORK, HOME)
    w.charge(ms(5, 19), 240, 20.0)  # home charge
    # Tue: home -> station, a 10 min stop (joined), station -> lake. 3 h at the lake.
    t = w.drive(ms(6, 9), 40, HOME, STATION)
    t = w.drive(t + 10 * MIN, 30, STATION, LAKE)
    w.drive(t + 180 * MIN, 60, LAKE, HOME)
    # Wed: a 40 min DC charge plus 15 min coffee on the way (joined).
    # At the lake: a 60 min destination charge, then parked 2 h (splits).
    t = w.drive(ms(7, 8), 40, HOME, STATION)
    w.charge(t, 40, 30.0)
    t = w.drive(t + 55 * MIN, 30, STATION, LAKE)
    w.charge(t, 60, 10.0)
    w.drive(t + 180 * MIN, 60, LAKE, HOME)
    # Thu: home -> work, a 20 min stop at home with a 15 min charge on the way back
    # ends the journey and the outing even though it is short.
    w.drive(ms(8, 8), 30, HOME, WORK)
    t = w.drive(ms(8, 17), 30, WORK, HOME)
    w.charge(t + 2 * MIN, 15, 3.0)
    w.drive(t + 20 * MIN, 40, HOME, STATION)
    # Sat: home -> work on a weekend is not a commute. Still out at the end of the data.
    w.drive(ms(10, 10), 30, HOME, WORK)
    return w
