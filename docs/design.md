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

## Storage and outputs

- The store (places, routes, labels, prices, answers, commands) is **one SQLite file** in a volume. Everything else is derived and recomputed.
- Outputs:
  - InfluxDB measurements written with the write user, like `ioniq_charges`: one row per trip, per journey and per month, and track points for maps. Grafana dashboards in homy read them.
  - A retained MQTT summary (the last 10 journeys, the month, and cost per kWh and per 100 km for all time and the last 30 days) for a Home Assistant view in homy.

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
