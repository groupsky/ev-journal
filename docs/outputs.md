# Outputs

Every run reads all of the history and recomputes everything. Each row is
written at the same timestamp every time, so a rerun overwrites and never
duplicates. Times are epoch milliseconds. All numbers are floats.

## Rules

- **Trip**: one `trip` row of `ioniq_sessions`.
- **Stop**: the time between two trips. Its charging time is the overlap with
  the `charge` rows of `ioniq_sessions`.
- **Journey** ends at a stop where the car is **more than 30 min not
  charging**, or at a stop **at home with a charge**. A short stop at home
  without a charge does not end the journey. Charges in the stops inside a
  journey are "on the way". A charge in the stop that ends a journey is at the
  destination; the journey ends on arrival.
- **Outing**: journeys from home until one ends at home. An outing that is
  not home yet is not written.
- **Place** of a trip end: the place whose radius holds the last real GPS
  fix of the trip, when that fix is at most 10 min before the end. The trip
  start works the same way with its first fix. Otherwise the place is
  unknown. A real fix has `provider = gps` and `acc_m <= 50`.
- **Place** of a stop or a charge: where the trip before it ended.
- **Commute**: a journey between a `home` and a `work` place that starts on
  Monday to Friday (local time, `EVJ_TZ`). Everything else is `other`.
  Holidays and routes come later.
- **Distance**: the length of the real GPS fixes of the trip. Steps under
  20 m are merged, so jitter while standing does not add km. A trip with
  fewer than 2 fixes uses homy's `distance_km` (`km_source = homy`).
- **Month**: local calendar month of the start time.

## InfluxDB (write user)

### `ev_trips` — one row per trip, at its start

| field | meaning |
|---|---|
| `end_ts` | end time, ms |
| `km` | distance |
| `km_source` | `gps` or `homy` |
| `kwh` | net battery kWh used (homy `energy_net_kwh`) |
| `waiting_sec`, `waiting_kwh` | from homy, when it gives them |
| `start_place`, `end_place` | place ids, when known |
| `estimated` | `true` when homy says `start_check = filled` |

### `ev_journeys` — one row per journey, at its start (the journey id)

| field | meaning |
|---|---|
| `end_ts` | arrival time, ms |
| `km` | sum of trip km |
| `drive_min` | time in trips |
| `waiting_min` | time not charging in the stops inside |
| `charge_min` | time charging in the stops inside |
| `charges` | number of charges on the way |
| `kwh_used` | sum of trip kWh |
| `kwh_charged` | kWh into the battery on the way |
| `trips` | number of trips |
| `label` | `commute` or `other` |
| `label_by_hand` | `true` when the label was set through the API |
| `not_me` | `true` when marked "not me" through the API |
| `start_place`, `end_place` | place ids, when known |

### `ev_outings` — one row per outing, at its start

`end_ts`, `km`, `kwh` (used), `journeys`.

### `ev_month` — one row per month, at its first ms (local time)

| field | meaning |
|---|---|
| `month` | `YYYY-MM` |
| `trips`, `journeys`, `outings` | counts, by start time |
| `km`, `kwh_used` | sum over the trips |
| `kwh_home`, `kwh_away`, `kwh_unknown` | kWh charged, by the place of the charge (unknown: no place) |
| `commute_km` | km of commute journeys |
| `odo_km` | odometer change in the month |
| `gap_pct` | (`km` − `odo_km`) / `odo_km` × 100 |

### `ev_track` — track points for Grafana's Geomap

Tag `journey` = the journey id. Fields `lat`, `lon`. The real fixes of the
journey, thinned to one point per 50 m.

### Stale rows

After writing, a run deletes the rows that exist but were not produced this
time. Example: two journeys merge, so the second one's row in `ev_journeys`,
its `ev_trips` row if the trip changed, and its `ev_track` series must go.

- The read user lists what exists: `SELECT` of one field per measurement,
  and `SHOW TAG VALUES FROM ev_track WITH KEY = "journey"`.
- The write user runs `DELETE FROM <m> WHERE time = <t>ms` and
  `DROP SERIES FROM ev_track WHERE journey = '<id>'`.
- Usually nothing is stale, so a run costs a few reads and no deletes.
- A run that produced no trips deletes nothing, so an empty read never
  wipes the history.
- Not covered: a track point that disappears inside a journey that still
  exists. Its series is kept, so the point stays.

**Rights.** In InfluxDB 1.x, `DELETE` and `DROP SERIES` need only `WRITE` on
the database, not admin. Checked on 2026-11-01: on the home server
(InfluxDB 1.12.4) the write user has `WRITE` on `homy` and is not admin, and
the read user has `READ`. CI runs the same steps against InfluxDB 1.12 with
a `WRITE`-only and a `READ`-only user (`tests/test_cleanup.py`).

## MQTT

Topic `homy/ioniq/journeys`, retained, QoS 1. JSON:

```json
{
  "updated": "2026-01-10T10:30:00Z",
  "journeys": [
    {"id": 1768039200000, "start": "…Z", "end": "…Z", "from": "Test Home", "to": "Test Work",
     "label": "other", "not_me": false, "km": 25.0, "kwh_used": 3.0, "drive_min": 30, "waiting_min": 0,
     "charge_min": 0, "charges": 0, "kwh_charged": 0.0}
  ],
  "month": {"month": "2026-01", "km": 624.1, "kwh_used": 36.0, "kwh_home": 23.0,
            "kwh_away": 40.0, "kwh_unknown": 0.0, "journeys": 10, "outings": 4, "commute_km": 99.5}
}
```

`journeys` holds the last 10, newest first. `month` is the current month.

## Places seed

`EVJ_PLACES_FILE` points to a JSON list outside the repo. It is imported once
into an empty SQLite store (`EVJ_DATA_DIR/ev-journal.sqlite`). After that the
places are changed through the [API](api.md):

```json
[{"id": "p-home", "name": "Test Home", "kind": "home", "lat": 0.1, "lon": 0.1, "radius_m": 150}]
```

`kind` is `home`, `work`, `station` or `other`. `radius_m` defaults to 150.
`aliases` (a list) and `destination` (a bool) are optional.
