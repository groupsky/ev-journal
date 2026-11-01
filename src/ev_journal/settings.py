"""Settings come only from the environment and from files it points to.

Nothing secret or private is stored in the repo.
"""

import os
from dataclasses import dataclass
from pathlib import Path


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
        )
