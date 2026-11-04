# ev-journal

`ev-journal` turns the car data that [homy](https://github.com/groupsky/homy)
stores in InfluxDB into trips, journeys and outings, with km, kWh and later a
price. It writes the results back to InfluxDB (for Grafana) and to MQTT (for
Home Assistant). The design is in [docs/design.md](docs/design.md).

## The code is public, the data is not

The data never enters this repo. That means places, place names, routes, GPS
tracks, receipts, prices paid and tokens. They live on the home server. Tests
use made-up places near 0° N 0° E (open sea). A test in CI fails when the repo
holds a coordinate outside that area.

## Settings

All settings come from the environment. Secrets come from files that the
environment points to.

| variable | meaning | default |
|---|---|---|
| `EVJ_INFLUX_URL` | InfluxDB 1.x URL | `http://influxdb:8086` |
| `EVJ_INFLUX_DB` | database | `homy` |
| `EVJ_INFLUX_READ_USER` | read user | — |
| `EVJ_INFLUX_READ_PASSWORD_FILE` | file with the read password | — |
| `EVJ_INFLUX_WRITE_USER` | write user | — |
| `EVJ_INFLUX_WRITE_PASSWORD_FILE` | file with the write password | — |
| `EVJ_MQTT_URL` | MQTT broker, for example `mqtt://broker:1883` | — |
| `EVJ_DATA_DIR` | folder for the SQLite store | `/data` |
| `EVJ_API_TOKEN_FILE` | file with the API token; no file, no API | — |
| `EVJ_PLACES_FILE` | places seed file ([format](docs/outputs.md#places-seed)) | — |
| `EVJ_TZ` | time zone for months and workdays | `UTC` |
| `EVJ_INTERVAL_SEC` | seconds between runs | `900` |
| `EVJ_API_HOST`, `EVJ_API_PORT` | where the [API](docs/api.md) listens | `0.0.0.0`, `8080` |
| `EVJ_SOC_TARGET` | SoC % a home charge aims for, in the live estimate | `80` |
| `EVJ_DRIFT_PCT` | drift limit for kWh per % and the meter ratio | `3` |
| `EVJ_DRIFT_KWH` | drift limit for the overhead per home charge | `0.5` |
| `EVJ_HOLIDAYS` | country code for official holidays ([forecast](docs/forecast.md)) | — |
| `EVJ_DAYS_OFF` | extra days off, `YYYY-MM-DD,…` | — |
| `EVJ_DEPARTURE` | departure, `HH:MM`, for every day type without its own; the charge ends by then | — |
| `EVJ_DEPARTURE_<TYPE>` | departure for one day type (`WORKDAY`, `WEEKEND`, `HOLIDAY` or an extra type), `HH:MM` or `none` (end at the night zone end) | — |
| `EVJ_DAY_TYPES` | extra day types by date range, `name:YYYY-MM-DD..YYYY-MM-DD,…` (both days included); each name has its own departure and forecast pool ([forecast](docs/forecast.md)) | — |
| `EVJ_TIMER_STEP_MIN` | step of the car's charge timer, minutes | `15` |
| `EVJ_RESERVE_PCT` | SoC % added to the bad-day need | `10` |
| `EVJ_PREHEAT` | `1` when a weekday preheat is set in the car | — |

## Run

- `ev-journal` (or `ev-journal loop`): recompute and write every `EVJ_INTERVAL_SEC`.
- `ev-journal run`: once.
- `ev-journal dry-run`: read only. Prints counts per month, the GPS km vs
  odometer gap, the station match counts and the back-tests.
  Writes nothing to InfluxDB or MQTT, works on a temporary copy of the
  store, and uses only the cached station feed.

Outputs and rules: [docs/outputs.md](docs/outputs.md). API: [docs/api.md](docs/api.md).
Away charges and their price: [docs/charges.md](docs/charges.md). Price of
trips and months: [docs/price.md](docs/price.md). SoC during a home charge
and battery model drift: [docs/home-charge.md](docs/home-charge.md).
Forecast and charging advice: [docs/forecast.md](docs/forecast.md).

## Develop

```sh
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
.venv/bin/ruff check . && .venv/bin/ruff format --check .
```

## Image

CI builds the image on every PR. On `main` it pushes
`ghcr.io/groupsky/ev-journal:latest` and a tag per commit.
