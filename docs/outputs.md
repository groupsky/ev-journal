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
| `preheat_kwh`, `preheat_eur` | wall kWh and € of the preheats on the plug before it ([design.md](design.md#heating)) |

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
| `preheat_kwh`, `preheat_eur` | wall kWh and € of the preheats that start in the month ([design.md](design.md#heating)) |

A field that is empty this run (`null`) is not written, so an old value stays.
`drift_flag` is always written, so an old flag is cleared.

### `ev_forecast` — one row per day, at its first ms (local time)

The forecast of each of the last 91 full days, made only from the days
before it, next to the real need ([forecast.md](forecast.md)). Fields
`day_type`, `normal_kwh`, `bad_kwh`, `real_kwh`.

### `ev_preheat` — one row per preheat on the plug, at its start

Tag `source`: `state` (the car app said `plugged-preheat`) or `meter` (the
charger meter fallback). Rules: [design.md](design.md#heating).

| field | meaning |
|---|---|
| `end_ts` | the start of its last minute with power or state, ms |
| `wall_kwh` | charger meter kWh |
| `battery_kwh` | what the pack gave: Δ `cum_out_kwh` − Δ `cum_in_kwh` |
| `outdoor_c` | the car's outside temperature at the start (30 min mean, at most 1 h old) |
| `zone` | tariff zone at the start: `day` or `night` |
| `journey` | the id of the journey it belongs to (the next one, when it starts within 1 h) |
| `in_charge` | the start of the home charge it lies inside, if any |
| `cost_eur` | wall kWh × home price of the zone + battery kWh × pool price |

### `ev_track` — track points for Grafana's Geomap

Tag `journey` = the journey id. Fields `lat`, `lon`. The real fixes of the
journey, thinned to one point per 50 m.

### Stale rows

After writing, a run deletes the rows that exist but were not produced this
time. Example: two journeys merge, so the second one's row in `ev_journeys`,
its `ev_trips` row if the trip changed, and its `ev_track` series must go.

- `ev_preheat` is checked the same way.
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
            "eur_per_kwh_away": null, "drift_flag": "", "preheat_kwh": 2.1,
            "preheat_eur": 0.4},
  "overview": {
    "all": {"eur_per_kwh": 0.2, "eur_per_kwh_home": 0.2, "eur_per_kwh_away": null,
            "eur_per_100km": 4.0, "kwh_per_100km": 20.0, "charges_no_cost": 0, "trips_no_price": 2},
    "last_30d": {"…": "the same fields"}
  },
  "home_charge": {"running": true, "since": "…Z", "soc_plugin": 30.0, "soc_now": 52.4,
                  "meter_kwh": 8.6, "target": 80.0, "meter_kwh_to_target": 9.4,
                  "min_to_target": 462},
  "preheat": {"start": "…Z", "end": "…Z", "source": "state", "wall_kwh": 2.1,
              "battery_kwh": 0.1, "outdoor_c": -3.5, "cost_eur": 0.4},
  "advice": {"day": "2026-01-11", "day_type": "weekend", "normal_kwh": 6.6, "bad_kwh": 14.2,
             "soc_now": 40.0, "target": 60, "level": "low", "start": "…", "end": "…",
             "departure": null, "capped": false, "preheat_off": false, "message": "Утре: …",
             "effects": [{"key": "month_mod2", "value": 1, "effect_kwh": 1.8}],
             "score": {"all": {"days": 91, "covered_pct": 92.3, "normal_error_kwh": 3.7}}},
  "upkeep": [{"name": "Test item", "status": "soon", "next_date": "2026-02-01",
              "next_km": 60000.0, "days_left": 22, "km_left": 4200.0}],
  "tyres": {"fitted": {"name": "Test set", "season": "summer", "age_years": 3.2},
            "hint": "winter", "age_warning": []}
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

`preheat` is the last preheat on the plug, as in `ev_preheat`, or `null`.

`upkeep` holds the car's upkeep items ([api.md](api.md#upkeep)): every item
that is `soon` or `overdue`, then the next 3 that are `ok`, the most urgent
first. `[]` when there are none.

`tyres` holds the tyre set on now (`fitted`: `name`, `season`,
`age_years`, or `null`), the season-swap `hint` (`winter`, `summer` or
`null`) and `age_warning`, the names of the sets that are too old
([api.md](api.md#tyres)).

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

- `upkeep`: an upkeep item ([api.md](api.md#upkeep)) is near or past its
  due date or km. One notice per threshold:
  - `upkeep:<item id>:<next_date>:<N>d`: `next_date` is `N` days away or
    less, for each `N` in `remind_days` (default 30 and 7). Only the nearest
    threshold is sent, so an item added 5 days before its date gets one
    notice, not two.
  - `upkeep:<item id>:<next_km>:<remind_km>km`: `km_left` is at or under
    `remind_km` (default 1 000).
  - `upkeep:<item id>:<next_date or next_km>:overdue:<ISO week>`: past the
    date (or else the km); repeats once a week until it is done.

  "Done" moves `next_date` and `next_km`, so the next cycle has new ids.
- `tyre_hint`: the season-swap hint ([api.md](api.md#tyres)); `id` is
  `tyre_hint:<winter or summer>:<local date>`. Sent when the hint appears,
  then at most once per 14 days while it stays. When the hint goes away the
  sent rows are dropped, so a hint that comes back is new. A run with fewer than
  `EVJ_TYRE_SWAP_DAYS` days of outside temperature sends no tyre notices
  and keeps the sent rows, so a gap in the readings is not a gone hint.
- `tyre_age`: a set (not retired) is older than `EVJ_TYRE_MAX_AGE_YEARS`;
  `id` is `tyre_age:<set id>`, sent once.

Upkeep and tyre notices are checked on every run and go out only between
08:00 and 21:00 local time; one due at night waits for the first run after
08:00. `dry-run` lists the ones it would send now.

### Home place

Topic `homy/ioniq/home`, **retained**, QoS 1. JSON:

```json
{"updated": "2026-01-10T10:00:00Z", "id": "p-home", "lat": 0.1, "lon": 0.1, "radius_m": 150}
```

The place of kind `home`: where the house charger meter is. homy reads it to
tell when the car leaves home; the store stays the one source. Published on
each run, and at once after a place is created, changed or deleted through
the API. Not published when there is no `home` place (an old retained message
stays). With several, the first by `id`, and a warning in the log.

## Places seed

`EVJ_PLACES_FILE` points to a JSON list outside the repo. It is imported once
into an empty SQLite store (`EVJ_DATA_DIR/ev-journal.sqlite`). After that the
places are changed through the [API](api.md):

```json
[{"id": "p-home", "name": "Test Home", "kind": "home", "lat": 0.1, "lon": 0.1, "radius_m": 150}]
```

`kind` is `home`, `work`, `station` or `other`. `radius_m` defaults to 150.
`aliases` (a list) and `destination` (a bool) are optional.
