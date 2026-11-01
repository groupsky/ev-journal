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

## Run

- `ev-journal` (or `ev-journal loop`): recompute and write every `EVJ_INTERVAL_SEC`.
- `ev-journal run`: once.
- `ev-journal dry-run`: read only. Prints counts per month and the GPS km vs
  odometer gap. Writes nothing to InfluxDB or MQTT.

Outputs and rules: [docs/outputs.md](docs/outputs.md). API: [docs/api.md](docs/api.md).
Away charges and their price: [docs/charges.md](docs/charges.md).

## Develop

```sh
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
.venv/bin/ruff check . && .venv/bin/ruff format --check .
```

## Image

CI builds the image on every PR. On `main` it pushes
`ghcr.io/groupsky/ev-journal:latest` and a tag per commit.
