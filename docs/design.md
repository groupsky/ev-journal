# Design

This is the design from [#1](https://github.com/groupsky/ev-journal/issues/1). The issues decide *what* gets built. This doc keeps the *why* and the contracts.

`ev-journal` turns the car data that the home automation stack ([homy](https://github.com/groupsky/homy)) already stores in InfluxDB into trips, journeys and outings, with places, km, kWh and a price. It also keeps a small store of places and routes with an HTTP API, and later gives a charging forecast and charging advice.


## Privacy rule

**The code is public. The data is private.** Places, place names, routes, GPS tracks, receipts, prices paid and voice commands are data. They live on the home server and never in this repo, its issues, PRs, test fixtures or screenshots. Tests use made-up places. Settings that carry data (the places seed, tokens) come from files or environment outside the repo.

## Inputs (all exist today)

- `ioniq_sessions` in InfluxDB (built by homy's `ioniq-sessions` bot): one row per `trip`, `charge` and `park`, with `start_ts`, `end_ts`, km, kWh, SoC, speed, `closed_by` and more.
- `ioniq` in InfluxDB: the raw car data, by `group` — `gps` (lat, lon, accuracy, provider), `bms/2101` (`soc`, `cum_in_kwh`, `cum_out_kwh`, `ignition`), odometer, gear, speed, ambient.
- `ioniq_charges` in InfluxDB (homy `energy-report`): one row per charge, home or away. For a home charge it has the kWh from the house charger meter and the cost with the day/night split. An away charge has only the car's own numbers.
- The house charger meter (`xymd1`, device `charger`).

Raw data in InfluxDB is kept forever. GPS: about 2,300 points per driving hour, about 6 % of them network fixes with 100–500 m accuracy.

## Four levels

| level | what | ends when |
|---|---|---|
| trip | one drive | the car is powered off, or a charge starts (built in homy) |
| stop | the time between two trips: parked, charging or both | — |
| journey | from A to B | a stop with **more than 30 min not charging** |
| outing | from home back to home, can span days | the car is home again |

- A charge on the way is part of the journey. Only the time *not charging* counts toward the 30 min. A 40 min DC charge plus a 15 min coffee stays inside the journey.
- A charge at the destination (the car stays parked after it) ends the journey on arrival. A home charge always ends the journey and the outing.
- A journey shows: km, drive time, waiting time, charge time, number of charges, kWh used, kWh charged on the way, and price.
- **Commute** = a journey between the home and work places (or along the saved route used for it) on a workday. Everything else is "other". A label can be changed by hand. A journey can be marked "not me", and the forecast then does not learn from it.

### Rules decided while building ([#3](https://github.com/groupsky/ev-journal/issues/3))

Accepted by the owner on 2026-11-01.

- A short stop at home **without** a charge does not end the journey.
- A stop at home **with** a charge always ends the journey and the outing, even under 30 min.
- An outing that is not home yet is not written.
- A trip end gets a place only from the trip's own GPS fix, at most 10 min before the end. Otherwise the place is unknown.
- A charge is at the place where the trip before it ended. A charge with no known place counts as `kwh_unknown`, not home or away.

## Distance

From the GPS track (real fixes only, accuracy 50 m or better), or from speed over time. The odometer (1 km steps) is a check: per month, the sum of trip km must be within 2 % of the odometer change. A trip whose start was filled (homy marks it) is marked "estimated".

## Price of a trip

- **Unit: battery kWh** from the lifetime counters `cum_in_kwh` / `cum_out_kwh`. In and out use the same meter, so charging losses go into the price by themselves.
- Price of a charge per battery kWh = money paid ÷ kWh into the battery. Home: the cost in `ioniq_charges`. Away: a receipt or the owner's answer (see the away-charges issue).
- **Average pool.** The battery is one pool with one average price. A charge mixes its kWh and cost in. A trip takes kWh out at the pool's price. At each charge the pool's kWh is set again from SoC × kWh per % (learned; about 0.30), so counter drift cannot build up.
- **Unknown share.** A charge without a price adds unknown kWh. A trip gets a price when the pool's unknown share is **10 % or less**: the known part sets the price and the trip is marked "estimated". Above 10 %: no price.
- Why not FIFO: FIFO only clears unknown energy faster. With about one charge a day, the unknown share falls under 10 % within about 3 charges anyway.
- Why not a price per %: SoC has 0.5 % steps, and the kWh in one % changes with temperature and age. Per % is the fallback only if the counters prove wrong.
- **Recompute from the whole history** on every run. The data is small (about 100 charges and 1,000 trips so far). A late receipt simply reprices everything.
- The month shows **spent** (charges paid) and **driving cost** (kWh used × price). They differ by the change in what the battery holds and by standby drain.

## Places, routes and the command log

- One store, used by the reports and by the car's voice assistant. The main copy is in `ev-journal`. The assistant keeps its own copy for offline use and syncs through a **regular HTTP API** with a token.
- **Place**: id, name, aliases, position, radius, kind (home, work, station, other), and an optional `destination` flag.
- **Route**: a name and an ordered list of place ids. A route is a plan, not history. A journey that follows a route gets the route's name. Route statistics (km, minutes, kWh by temperature) feed the forecast.
- **New places**: frequent stops without a name are offered for naming.
- **Command log**: the assistant POSTs each voice command, with an offline queue. A navigate or multi-stop command gives the journey's plan at its start.

## Upkeep

- The car's **papers** run out on a date (technical inspection, insurance,
  vignette and so on). **Service items** are due after some months, some km,
  or both, whichever comes first ([#44](https://github.com/groupsky/ev-journal/issues/44)).
- They live in the same SQLite file (`upkeep_items`, with the sync rules of
  places, and `upkeep_log`, one row per "done"). The car app and the voice
  assistant read them through the API ([api.md](api.md#upkeep)); the run
  sends reminders as notices ([outputs.md](outputs.md#notices)).
- The km come from the last odometer reading in InfluxDB, read on each
  request. Status is `overdue`, `soon` (inside the first reminder days or
  the reminder km) or `ok`.
- The real items, dates and km are data. They are entered through the API
  and never go into the repo.

## Tyres

- The car can have more than one tyre set. A **swap** puts a set on at a
  date and an odometer km; the set on before comes off at the same moment
  ([#45](https://github.com/groupsky/ev-journal/issues/45)).
- They live in the same SQLite file (`tyre_sets`, with the sync rules of
  places, and `tyre_swaps`, the swap log). The km of a set are the sum of
  the odometer change over each time it was on; the set on now runs to the
  last odometer reading. A set with swaps is retired, not deleted, so its
  km stay.
- The age comes from the DOT date code (`WWYY`), counted from the Monday of
  that week. A set older than `EVJ_TYRE_MAX_AGE_YEARS` gets a warning.
- The **season hint** uses the car's outside temperature (`ioniq` groups
  `ambient` field `c` and `hvac` field `outdoor_c`), one mean per local day
  over the last 30 days (3 times `EVJ_TYRE_SWAP_DAYS` when that is longer). The car reads only while it runs, so a day with no
  reading is left out. The mean of the last 7 such days below 7 °C says
  "winter"; above says "summer". The same mean is used both ways: it is
  simple and one warm or cold day does not flip it.
- The real sets, DOT codes and swap dates are data, entered through the API.

## Health series

The car app draws battery health trends from `/health/daily` and
`/health/fan` ([#49](https://github.com/groupsky/ev-journal/issues/49),
[api.md](api.md#health-series)).

- **Why from here:** the raw car data in InfluxDB is kept forever and covers
  every drive. The box in the car sees only what it read while it was on, and
  loses it on a reinstall. ev-journal already reads that InfluxDB, and the car
  app already uses this API with the same token. Not chosen: the box asking
  InfluxDB itself (it would need database passwords), and Grafana (not usable
  on the car screen, nor offline).
- **Rules per local day** (`ioniq` measurement, by `group`):
  - `soh_max`: the highest `soh` of `bms/2105`. The value moves on single
    frames, so the highest is the steady one.
  - `isolation_min_kohm`: the lowest `isolation_kohm` of `bms/2101`.
  - `cell_spread_mv`: the median of `cell_max_v − cell_min_v` over the
    `bms/2101` frames with |`hv_a`| below 5 A, so quiet frames are compared
    with quiet frames. `cell_spread_mv_max`: the highest, over all frames.
  - `module_temp_spread_c_max`: the highest `value` of homy's
    `derived/module_temp_spread_c`.
  - `aux_12v_rest`: the first valid `aux_12v` of the day that comes 6 h or
    more after the valid one before it (a park of at least 6 h, before the
    12 V charger lifts the voltage). With no valid reading in the 30 days
    before, the first reading counts too.
  - Fan: per month and `fan_status` step 1–9, the frame count and the median
    `fan_feedback`.
- **Garbage frames:** a value outside its physical bounds is dropped. Cell
  voltage 1–5 V and module temperatures −40–100 °C (so a spread of 0–140 °C)
  are the bounds of homy's `ioniq-cell-health` bot; a cell pair with max
  below min is dropped too, as there. The others are plain sanity limits:
  SoH 50–110 %, isolation 1–10 000 kΩ (0 means "not measured"), 12 V
  8–16 V, fan feedback 0–255. The numbers are in `health.py`.
- **Load and cache:** past days do not change, so each day before yesterday
  is read once, in one call per month (InfluxDB is read day by day inside it, and a day that hits the row limit is read in halves), and kept in the SQLite
  `health_days` table (a day without car data too, as an empty row).
  Yesterday and today are read on each request and not kept. Raw frames are
  read, not InfluxDB aggregates, so the rules stay in Python and the tests
  check them with made-up frames.

## Heating

The cost of a preheat on the plug, and "preheat on the plug or heat while
driving?" ([#47](https://github.com/groupsky/ev-journal/issues/47)).

- **Limits.** The car gives no heater power value, so the cabin heater's kWh
  while driving is not known here (a separate estimate, later). What is
  measured: the preheat on the plug (the charger meter and the battery
  counters), and whole mornings with and without a preheat.
- **A preheat** is a run of minutes with a car frame in `state =
  plugged-preheat`, joined when the gap is under 5 min. **Fallback** when the
  car app was not awake: a charger meter block above 150 W after a home
  charge (per `ioniq_sessions`) has ended and before the next trip, with no
  SoC rise, and not at the time of a known preheat. The fallback looks at most
  24 h after the charge.
- **Numbers per preheat:** wall kWh = meter total at its last minute minus
  the total before its first; battery kWh = Δ `cum_out_kwh` − Δ `cum_in_kwh`;
  outside °C = the car's `hvac outdoor_c` and `ambient c`, 30 min mean, at
  most 1 h before the start; it belongs to the next journey.
- **Cost** = wall kWh × the home price of its tariff zone (day or night, as
  in [forecast.md](forecast.md)) + battery kWh × the pool price at the next
  journey's first trip ([price.md](price.md)). The home price per zone is
  Σ `cost_<zone>_eur` ÷ Σ `meter_kwh_<zone>` of the home charges in
  `ioniq_charges` over the last 30 days.
- **Not counted twice.** A preheat inside a home charge (in homy's data a
  preheat could look like a charge) has its wall kWh in that charge's meter
  kWh and cost already. Its wall € is taken out of the charge's cost before
  the trips are priced, so it raises neither the driving price nor
  `spent_eur`; it shows only as heating. `ev_preheat.in_charge` marks it.
- **Preheat or not** (`/heating/compare`): commute journeys in 5 °C bands of
  the outside temperature at the start, with and without a preheat. Means
  only from 3 journeys up. `dry-run` prints the same table without money.
- **Battery heater.** The car gives `heater_1` and `heater_2` in `bms/2105`
  and no power. `dry-run` counts the 10 min buckets where a heater reading is
  more than 2 °C above the warmest module (`temp_max` of `bms/2101`), and how
  many fall in preheats and home charges. The answer from the real data, as
  counts, goes in [heating.md](heating.md).

## Storage and outputs

- The store (places, routes, upkeep items, tyre sets and swaps, labels, the health days cache, prices, answers, commands, sent notice ids) is **one SQLite file** in a volume. Everything else is derived and recomputed.
- Outputs:
  - InfluxDB measurements written with the write user, like `ioniq_charges`: one row per trip, per journey and per month, and track points for maps. Grafana dashboards in homy read them.
  - A retained MQTT summary (the last 10 journeys, the month, cost per kWh and per 100 km for all time and the last 30 days, the upkeep items that are due, and the tyre set on now with its hint) for a Home Assistant view in homy.
  - Notices on MQTT (not retained), for example the evening plug check; homy sends them on to a person ([outputs.md](outputs.md#notices)).
  - The home place on MQTT (retained), so homy can tell when the car leaves home ([outputs.md](outputs.md#home-place)).

## Later

- **Home charge SoC estimate**: SoC during a home charge from the house meter, the SoC at plug-in and the temperatures, learned from past charges; and tracking of the battery model (kWh per %, meter-to-battery ratio) to find deviations.
- **Forecast for tomorrow and charging advice**: day type (workday, weekend, holiday), planned trips, the temperature forecast and past outings. The goal is **battery life first**: charge only to the % needed plus a reserve, end close to departure, keep a lower % for long parks, and choose the power level for timing (end inside the night tariff zone).
- **Contactor control**: the car can limit charging only by hours, not by %. A contactor on the home charger supply lets software stop the charge at the needed %. When the automation is down, the car charges.
- **Home reader**: an indoor phone that reads the car during home charges, for live SoC. It lives in the logger app, not here.

## Not being built

Business km logs, driver detection, any public view of the data, and live station availability (only if a free source gives it).

## Steps

- [#2](https://github.com/groupsky/ev-journal/issues/2) Bootstrap: package, CI, image, privacy guard
- [#3](https://github.com/groupsky/ev-journal/issues/3) Journeys and outings from `ioniq_sessions`
- [#4](https://github.com/groupsky/ev-journal/issues/4) Places, routes, journey labels and command log API
- [#5](https://github.com/groupsky/ev-journal/issues/5) Price of trips and journeys
- [#8](https://github.com/groupsky/ev-journal/issues/8) Home charge SoC estimate and battery model drift ([home-charge.md](home-charge.md))
- [#9](https://github.com/groupsky/ev-journal/issues/9) Forecast for tomorrow and charging advice ([forecast.md](forecast.md))
