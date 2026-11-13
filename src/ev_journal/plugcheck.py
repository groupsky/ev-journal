"""The evening plug check (#43).

At EVJ_PLUG_CHECK_AT a notice goes out when a charge is needed, the car is
at home and it is not known to be plugged in. Rules: docs/forecast.md#plug-check.
"""

import sqlite3
from collections.abc import Callable
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from ev_journal.homecharge import ON_W

KIND = "plug_check"
TEXT_OUT = "Колата не е включена за зареждане."
TEXT_UNKNOWN = "Не виждам дали колата е включена."


def next_at(now: int, hhmm: str, tz: ZoneInfo) -> int:
    """The next time (ms) after now when the local clock shows hhmm."""
    local = datetime.fromtimestamp(now / 1000, tz)
    at = datetime.combine(local.date(), time.fromisoformat(hhmm), tz)
    if at.timestamp() * 1000 <= now:
        at = datetime.combine(local.date() + timedelta(days=1), time.fromisoformat(hhmm), tz)
    return int(at.timestamp() * 1000)


def plug_state(
    frames: list[tuple[int, bool]], meter_w: list[float | None], charge_row: bool = False
) -> str:
    """plugged, out or unknown, from what was seen since the trip end: the car
    frames ((time, cable in)), the charger meter power (W), and whether a
    charge row of ioniq_sessions starts (homy opens it at plug-in).

    Charging power or a charge row says plugged. No power says nothing: the
    car's timer may start later. Else the last frame decides; none: unknown."""
    if charge_row or any(w is not None and w > ON_W for w in meter_w):
        return "plugged"
    if not frames:
        return "unknown"
    return "plugged" if max(frames)[1] else "out"


def decide(advice: dict | None, at_home: bool, state: str) -> tuple[str | None, str]:
    """(notice text or None, the reason)."""
    if not advice or advice.get("level") is None:
        return None, "no charge needed"
    if not at_home:
        return None, "the car is not at home (or the place is unknown)"
    if state == "plugged":
        return None, "the car is plugged in"
    head = TEXT_OUT if state == "out" else TEXT_UNKNOWN
    return f"{head} {advice['message']}", f"charge needed, car at home, plug {state}"


def notice(evening: str, t_iso: str, text: str) -> dict:
    """The notice for one evening (local date). Its id is stable for that evening."""
    return {"id": f"{KIND}:{evening}", "kind": KIND, "time": t_iso, "text": text}


def send(db: sqlite3.Connection, n: dict, publish: Callable[[dict], None], now: int) -> bool:
    """Publish a notice once: an id in the store is never sent again."""
    if db.execute("SELECT 1 FROM notices WHERE id = ?", (n["id"],)).fetchone():
        return False
    publish(n)
    db.execute("INSERT INTO notices (id, time) VALUES (?, ?)", (n["id"], now))
    db.commit()
    return True
