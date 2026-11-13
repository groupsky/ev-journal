"""Settings come only from the environment and from files it points to.

Nothing secret or private is stored in the repo.
"""

import os
from dataclasses import dataclass, field
from datetime import date, time
from pathlib import Path

# Every env name with this prefix is read as a departure of one day type.
DEPARTURE_PREFIX = "EVJ_DEPARTURE_"
BASE_DAY_TYPES = ("workday", "weekend", "holiday")


def _read_file(path: str | None) -> str | None:
    if not path:
        return None
    return Path(path).read_text().strip()


@dataclass(frozen=True)
class Settings:
    influx_url: str
    influx_db: str
    influx_read_user: str | None
    influx_read_password: str | None
    influx_write_user: str | None
    influx_write_password: str | None
    mqtt_url: str | None
    data_dir: Path
    api_token: str | None
    places_file: Path | None
    tz: str
    interval_sec: int
    api_host: str
    api_port: int
    soc_target: float = 80.0
    drift_pct: float = 3.0
    drift_kwh: float = 0.5
    holidays_country: str | None = None
    days_off: tuple[str, ...] = ()
    departure: str | None = None
    reserve_pct: float = 10.0
    preheat: bool = False
    timer_step_min: int = 15
    departures: dict[str, str | None] = field(default_factory=dict)  # day type -> HH:MM
    day_ranges: tuple[tuple[str, date, date], ...] = ()  # (day type, first, last)
    plug_check_at: str | None = "22:30"  # HH:MM local; None: no plug check (#43)
    tyre_max_age_years: float = 6.0  # (#45)
    tyre_swap_c: float = 7.0
    tyre_swap_days: int = 7

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "Settings":
        e = os.environ if env is None else env
        return cls(
            influx_url=e.get("EVJ_INFLUX_URL", "http://influxdb:8086"),
            influx_db=e.get("EVJ_INFLUX_DB", "homy"),
            influx_read_user=e.get("EVJ_INFLUX_READ_USER"),
            influx_read_password=_read_file(e.get("EVJ_INFLUX_READ_PASSWORD_FILE")),
            influx_write_user=e.get("EVJ_INFLUX_WRITE_USER"),
            influx_write_password=_read_file(e.get("EVJ_INFLUX_WRITE_PASSWORD_FILE")),
            mqtt_url=e.get("EVJ_MQTT_URL"),
            data_dir=Path(e.get("EVJ_DATA_DIR", "/data")),
            api_token=_read_file(e.get("EVJ_API_TOKEN_FILE")),
            places_file=Path(e["EVJ_PLACES_FILE"]) if e.get("EVJ_PLACES_FILE") else None,
            tz=e.get("EVJ_TZ", "UTC"),
            interval_sec=int(e.get("EVJ_INTERVAL_SEC", "900")),
            api_host=e.get("EVJ_API_HOST", "0.0.0.0"),
            api_port=int(e.get("EVJ_API_PORT", "8080")),
            soc_target=float(e.get("EVJ_SOC_TARGET", "80")),
            drift_pct=float(e.get("EVJ_DRIFT_PCT", "3")),
            drift_kwh=float(e.get("EVJ_DRIFT_KWH", "0.5")),
            holidays_country=e.get("EVJ_HOLIDAYS") or None,
            days_off=tuple(d.strip() for d in e.get("EVJ_DAYS_OFF", "").split(",") if d.strip()),
            departure=_hhmm(e.get("EVJ_DEPARTURE") or None),
            reserve_pct=float(e.get("EVJ_RESERVE_PCT", "10")),
            preheat=e.get("EVJ_PREHEAT", "") in ("1", "true", "yes"),
            timer_step_min=_positive(e, "EVJ_TIMER_STEP_MIN", "15"),
            departures={
                k[len(DEPARTURE_PREFIX) :].lower(): _hhmm(_none(v))
                for k, v in e.items()
                if k.startswith(DEPARTURE_PREFIX) and len(k) > len(DEPARTURE_PREFIX)
            },
            day_ranges=_ranges(e.get("EVJ_DAY_TYPES", "")),
            plug_check_at=_hhmm(_none(e.get("EVJ_PLUG_CHECK_AT", "22:30"))),
            tyre_max_age_years=float(e.get("EVJ_TYRE_MAX_AGE_YEARS", "6")),
            tyre_swap_c=float(e.get("EVJ_TYRE_SWAP_C", "7")),
            tyre_swap_days=_positive(e, "EVJ_TYRE_SWAP_DAYS", "7"),
        )._checked()

    def _checked(self) -> "Settings":
        known = set(BASE_DAY_TYPES) | {name for name, _a, _b in self.day_ranges}
        unknown = set(self.departures) - known
        if unknown:
            raise ValueError(f"EVJ_DEPARTURE_<TYPE>: no such day type: {sorted(unknown)}")
        return self


def _hhmm(v: str | None) -> str | None:
    """A departure time HH:MM, checked at start-up."""
    if v is not None:
        time.fromisoformat(v)
    return v


def _none(v: str) -> str | None:
    return None if v.lower() == "none" else v


def _ranges(v: str) -> tuple[tuple[str, date, date], ...]:
    """Extra day types: "name:YYYY-MM-DD..YYYY-MM-DD,..." (both days included)."""
    out = []
    for item in v.split(","):
        if not item.strip():
            continue
        name, _, span = item.strip().partition(":")
        first, _, last = span.partition("..")
        a, b = date.fromisoformat(first), date.fromisoformat(last)
        if not name or name.lower() in BASE_DAY_TYPES or a > b:
            raise ValueError(f"EVJ_DAY_TYPES: bad range {item!r}")
        out.append((name.lower(), a, b))
    return tuple(out)


def _positive(e, key: str, default: str) -> int:
    v = e.get(key, default)
    n = int(v)
    if n <= 0:
        raise ValueError(f"{key} must be above 0, not {v!r}")
    return n
