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
  fix of the trip, when that fix is at most 10 min before the end. When
  several places hold it, a `home` place wins; otherwise the nearest. The trip
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
| `cost_eur`, `price_eur_per_kwh`, `price_estimated`, `unknown_share` | the price ([price.md](price.md)) |

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
| `cost_eur`, `price_estimated` | the price ([price.md](price.md)) |
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
| `spent_eur`, `driving_cost_eur`, `drain_kwh`, `eur_per_100km`, `kwh_per_100km` | the price ([price.md](price.md)) |
| `eur_per_kwh`, `eur_per_kwh_home`, `eur_per_kwh_away` | money paid per kWh charged ([price.md](price.md#overview)) |
| `odo_km` | odometer change in the month |
| `gap_pct` | (`km` − `odo_km`) / `odo_km` × 100 |
| `kwh_per_pct`, `meter_ratio`, `overhead_kwh` | the battery model of the month's home charges ([home-charge.md](home-charge.md#drift)) |
| `drift_flag` | why a value of the model moved against its 3-month median; empty when none did |

A field that is empty this run (`null`) is not written, so an old value stays.
`drift_flag` is always written, so an old flag is cleared.

### `ev_forecast` — one row per day, at its first ms (local time)

The forecast of each of the last 91 full days, made only from the days
before it, next to the real need ([forecast.md](forecast.md)). Fields
`day_type`, `normal_kwh`, `bad_kwh`, `real_kwh`.

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
     "from_place_id": "1", "to_place_id": "2",
     "label": "other", "not_me": false, "km": 25.0, "kwh_used": 3.0, "drive_min": 30, "waiting_min": 0,
     "charge_min": 0, "charges": 0, "kwh_charged": 0.0, "cost_eur": null}
  ],
  "month": {"month": "2026-01", "km": 624.1, "kwh_used": 36.0, "kwh_home": 23.0,
            "kwh_away": 40.0, "kwh_unknown": 0.0, "journeys": 10, "outings": 4, "commute_km": 99.5,
            "spent_eur": 0.0, "driving_cost_eur": 0.0, "eur_per_100km": null,
            "kwh_per_100km": 5.8, "eur_per_kwh": null, "eur_per_kwh_home": null,
            "eur_per_kwh_away": null, "drift_flag": ""},
  "overview": {
    "all": {"eur_per_kwh": 0.2, "eur_per_kwh_home": 0.2, "eur_per_kwh_away": null,
            "eur_per_100km": 4.0, "kwh_per_100km": 20.0, "charges_no_cost": 0, "trips_no_price": 2},
    "last_30d": {"…": "the same fields"}
  },
  "home_charge": {"running": true, "since": "…Z", "soc_plugin": 30.0, "soc_now": 52.4,
                  "meter_kwh": 8.6, "target": 80.0, "meter_kwh_to_target": 9.4,
                  "min_to_target": 462},
  "advice": {"day": "2026-01-11", "day_type": "weekend", "normal_kwh": 6.6, "bad_kwh": 14.2,
             "soc_now": 40.0, "target": 60, "level": "low", "start": "…", "end": "…",
             "departure": null, "capped": false, "preheat_off": false, "message": "Утре: …",
             "effects": [{"key": "month_mod2", "value": 1, "effect_kwh": 1.8}],
             "score": {"all": {"days": 91, "covered_pct": 92.3, "normal_error_kwh": 3.7}}}
}
```

`journeys` holds the last 10, newest first. `month` is the current month.
`overview` is cost per kWh and per 100 km for all time and the last 30 days
([price.md](price.md#overview)).

`advice` is tomorrow's forecast and charging advice ([forecast.md](forecast.md)),
or `null`. `start`, `end` and `departure` are local ISO times; `level` is
`null` when no charge is needed. `score` holds the back-test per day type and
`all` (extra day types count as workdays there). `effects` lists the
calendar rhythms the forecast uses and what each adds for that day, in kWh
(0 when the day's value has no effect); `[]` when today's rule is used
([forecast.md](forecast.md#rhythms-effects)).

`home_charge` is the SoC estimate of a home charge since the car last came
home ([home-charge.md](home-charge.md)), or `null`. `running` is `false`
when the meter has shown no charging for 2 min; `min_to_target` is then
`null`.

### Notices

Topic `homy/ioniq/notice`, QoS 1, **not retained**. JSON:

```json
{"id": "plug_check:2026-01-10", "kind": "plug_check", "time": "2026-01-10T22:30:00+02:00",
 "text": "Колата не е включена за зареждане. Утре: …"}
```

A message for a person; homy sends it on (Telegram). `id` is the same for
every copy of one notice, so a receiver can drop a repeat; ev-journal never
sends the same `id` twice, also after a restart. `time` is local ISO time.
`text` is Bulgarian. Kinds:

- `plug_check`: the evening plug check ([forecast.md](forecast.md#plug-check));
  `id` is `plug_check:<local date>`.

Later notices (upkeep reminders) use the same topic with their own `kind`.

## Places seed

`EVJ_PLACES_FILE` points to a JSON list outside the repo. It is imported once
into an empty SQLite store (`EVJ_DATA_DIR/ev-journal.sqlite`). After that the
places are changed through the [API](api.md):

```json
[{"id": "p-home", "name": "Test Home", "kind": "home", "lat": 0.1, "lon": 0.1, "radius_m": 150}]
```

`kind` is `home`, `work`, `station` or `other`. `radius_m` defaults to 150.
`aliases` (a list) and `destination` (a bool) are optional.
