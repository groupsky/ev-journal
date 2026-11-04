"""Calendar keys: each maps a day to a value (#23).

A forecast pool can take only past days with the same value. The keys know
nothing about pools, so other forecasts can reuse them.
"""

from datetime import date


def weekday(d: date, dtype: str) -> int:
    return d.weekday()  # 0 Monday … 6 Sunday


def iso_week_parity(d: date, dtype: str) -> int:
    return d.isocalendar().week % 2


def month_parity(d: date, dtype: str) -> int:
    return d.month % 2


def month_part(d: date, dtype: str) -> str:
    """first (1–10), middle (11–20) or last (21–31) part of the month."""
    return "first" if d.day <= 10 else "middle" if d.day <= 20 else "last"


def month(d: date, dtype: str) -> int:
    return d.month


def day_type(d: date, dtype: str) -> str:
    """workday, weekend, holiday or a named range of EVJ_DAY_TYPES."""
    return dtype


KEYS = {
    f.__name__: f for f in (weekday, iso_week_parity, month_parity, month_part, month, day_type)
}

# How many days back a pool with the key looks: enough days with the same value.
WINDOW = {
    "weekday": 84,
    "iso_week_parity": 56,
    "month_parity": 122,
    "month_part": 91,
    "month": 365,
    "day_type": 365,
}


def value(choice: tuple[str, ...], d: date, dtype: str) -> tuple:
    """The values of the keys of a choice for a day."""
    return tuple(KEYS[k](d, dtype) for k in choice)


def window(choice: tuple[str, ...], base: int) -> int:
    """The window of a choice: the longest of its keys; base for no key."""
    return max((WINDOW[k] for k in choice), default=base)
