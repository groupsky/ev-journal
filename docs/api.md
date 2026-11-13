# API

The full contract is [openapi.json](openapi.json). Regenerate it after a
change with `python -m ev_journal.api > docs/openapi.json`; a test fails when
it is out of date, and every response in the tests is checked against it.

The API runs inside `ev-journal loop` when `EVJ_API_TOKEN_FILE` is set. It
listens on `EVJ_API_HOST:EVJ_API_PORT` (default `0.0.0.0:8080`). Publish it
only on the private network.

## Auth

Every request needs `Authorization: Bearer <token>`. The token is the content
of `EVJ_API_TOKEN_FILE`, outside the repo. Without a configured token every
request gets `401`.

## Places and routes

| method | path | does |
|---|---|---|
| `GET` | `/places` | all places; with `?changed_since=T` the changed ones and delete markers |
| `POST` | `/places` | create; the server picks the id |
| `GET` | `/places/{id}` | one place |
| `PUT` | `/places/{id}` | change |
| `DELETE` | `/places/{id}` | delete (leaves a delete marker for sync) |
| `GET` | `/places/suggestions` | frequent stops with no place |

Routes (`/routes`, `/routes/{id}`) work the same. A route is a name and an
ordered list of existing place ids. A place that a route uses cannot be
deleted (`409`); change or delete the route first.

**Errors** are always `{"detail": "<text>"}`: `401` no or wrong token, `404`
not found, `409` conflict, `422` a bad request.

**Sync.** Every write moves a row to the end of a sequence. A list returns
`cursor`. `GET …?changed_since=<cursor>` returns the rows written after it,
with deleted ones as `deleted` markers, and the next `cursor`. Start with a
plain `GET` (all rows) or `changed_since=0`. The cursor is a counter, not a
time, so a clock change or two writes in the same ms lose nothing.

**Versions.** Every write returns the new `version`. Send the version you saw
in the body (`PUT`) or as `?version=` (`DELETE`). There is one user, so the
last write wins. When the version you sent was old, the answer has
`"stale": true`, so the client can tell the owner.

**Seed.** `EVJ_PLACES_FILE` is imported once, into an empty store. After that
the API owns the places.

## Suggestions

Journey ends (stops of more than 30 min) that lie in no place are grouped
within 100 m. A group with 3 or more visits is a suggestion: position, visits
and the median local hour of arrival. To name one, `POST /places` at its
position; it then stops being suggested. Suggestions come from the last
recompute, so they are empty until the first run is done.

## Journey labels

`PUT /journeys/{id}/label` with `{"label": "commute" | "other" | null,
"not_me": bool}`. The id is a time in ms, usually the journey's start; the
label attaches to the journey that contains that time, so it survives every
recompute. A time in no journey of the last recompute gets `404` (also before
the first recompute is done). `null` means "use the computed label". `ev_journeys` then has
`label_by_hand` and `not_me`.

## Journey list

`GET /journeys?before=<ms>&limit=<n>` gives journeys of the last recompute,
newest first. Each item has the same fields as the items of `journeys` in the
retained MQTT summary (`id`, `start`, `end`, `from`, `to`, `from_place_id`,
`to_place_id`, `label`, `not_me`, `km`, `kwh_used`, `drive_min`, `waiting_min`, `charge_min`, `charges`,
`kwh_charged`, `cost_eur`). The MQTT summary holds only the last 10; this is
for scrolling further back.

- `before` (optional): only journeys whose id (start, ms) is smaller. Without
  it, the list starts at the newest journey.
- `limit`: 1 to 100, default 20.
- For the next page, pass the `id` of the last item as `before`. The pages
  then join with no gap and no duplicate. An empty list means no older
  journeys. Before the first recompute is done, the list is empty.

Filters (all optional). They apply together, and together with `before`:

- `from`, `to` (ms): only journeys whose start (`id`) is in the range. `to`
  is inclusive and `before` is exclusive; when both are given, both apply.
  `to` alone jumps to a date: the list starts at the last journey on or
  before it.
- `with_charge=true|false`: only journeys with a charge, or only without.
- `min_km`: only journeys with `km` of at least this value.
- `place=<place id>`: only journeys that start or end at that place. It
  matches the place id (`from_place_id`, `to_place_id`), not the name, so two
  places with the same name stay apart. A journey whose end is not a known
  place has `null` there.

## Journey days

`GET /journeys/days?from=<ms>&to=<ms>` gives one row per local day that has
journeys, newest first: `day` (`YYYY-MM-DD`), `count`, `km`, `kwh_used`,
`kwh_charged` and `cost_eur`. It takes the same filters as `GET /journeys`
(all optional; no paging). A journey counts on the local day of its start, in
the configured time zone (`EVJ_TZ`), so a day can be 23 or 25 h long on a DST
change. The totals equal the sum of the `GET /journeys` items for the same
filters. `cost_eur` is the sum of the journeys that have a price, and `null`
when none of them has one. A client builds year, month and day headers,
totals and a jump bar from it.

## Journey track

`GET /journeys/{id}/track` gives the route of a journey for a map. The id is
the journey's start in ms (as in `ev_journeys`). The answer is a list of
points `{"t", "lat", "lon", "kwh_per_100km"}`, in time order:

- The points are the journey's real GPS fixes (accuracy 50 m or better),
  thinned like `ev_track`: a fix is kept when it is 50 m or more from the last
  kept one, and the last fix is kept too.
- `kwh_per_100km` is for the part since the point before. It comes from the
  battery counters (`cum_out_kwh` − `cum_in_kwh`), read from InfluxDB on each
  request. The counters have 0.1 kWh steps, so the energy is a straight line
  between the readings where the value changes. A 50 m part then shows the
  real rate and not 0 or a whole step. It is `null` for the first point, and
  for a part with no reading at its start or end or with two readings more
  than 60 s apart inside it.
- A journey not in the last recompute gets `404`. A journey with no real
  fixes gets `[]`.

## Command log

`POST /commands` with `client_id`, `time`, `text`, `action` and an optional
`place_id` or `route_id`. The client queues offline and sends later. The first
post of a `client_id` returns `201`; a repeat returns `200` with the stored
command and changes nothing.

## Away charges

`POST /charges/{id}/receipt`, `GET /questions`,
`POST /questions/{id}/answer` and `GET /charges/price-estimate`: see
[charges.md](charges.md).

## Upkeep

The car's papers (run out on a date) and service items (due by months, by
km, or both, whichever comes first). See [design.md](design.md#upkeep).

| method | path | does |
|---|---|---|
| `GET` | `/upkeep` | `km_now`, `km_now_time` and all items with their status, the most urgent first |
| `POST` | `/upkeep` | create; the server picks the id |
| `GET` | `/upkeep/{id}` | one item with its status |
| `PUT` | `/upkeep/{id}` | change |
| `DELETE` | `/upkeep/{id}` | delete (a delete marker, like places) |
| `POST` | `/upkeep/{id}/done` | log that it was done; returns the item |
| `GET` | `/upkeep/{id}/log` | what was done, newest first |

An item: `name`, `kind` (`paper` or `service`), `due_date` (a paper),
`every_months` and `every_km` (a service item, either or both), `last_date`,
`last_km`, `remind_days` (default `[30, 7]`), `remind_km` (default `1000`),
`note`. Dates are `YYYY-MM-DD`. Errors and versions work as for places.

Each item also gets:

- `next_date`: `due_date` for a paper; `last_date` + `every_months` for a
  service item (the 31st becomes the last day of a short month).
- `next_km`: `last_km` + `every_km`.
- `days_left` (from today, local time) and `km_left` (from `km_now`, the last
  odometer reading in InfluxDB). `km_left` is `null` when there is no
  odometer reading yet; the date still works.
- `status`: `overdue` when past the date or the km; `soon` within
  `remind_days[0]` days or `remind_km` km; else `ok`. Whichever comes first
  decides.

`POST /upkeep/{id}/done` takes `{"date"?, "km"?, "due_date"?, "note"?}`.
`date` defaults to today and `km` to `km_now`. It writes a log row and sets
`last_date` and `last_km` (with no km at all, `last_km` stays as it was).
When the odometer cannot be read, `km_now` is `null`, as with no reading. A paper needs the new `due_date` (`422`
without it); it replaces the old one.

## Advice

`GET /advice`: tomorrow's forecast and charging advice from the last
recompute, the same object as `advice` in the MQTT summary
([forecast.md](forecast.md)). `404` before the first recompute, or when
there is no forecast (too little history, or no SoC known).

`GET /advice?soc=<SoC % now>&km_home=<km to home>`: the same advice made
again on request, for a client in the car on the way home
([forecast.md](forecast.md#advice-on-request)). Both are optional; with
either one, the response adds `soc_arrival` (the SoC expected at home) and
`computed_at` (now), and the `message` starts with the arrival SoC. Without
`soc`, the SoC of the last recompute is used. `soc` is 0–100, `km_home` is
0 or more; other values give `422`. Without parameters `computed_at` and
`soc_arrival` are `null`.

The client finds home in `GET /places`: the place with `"kind": "home"`.
It measures `km_home` from there (a straight line is fine; see the detour
factor).
