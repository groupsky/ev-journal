# Price

The price of each trip, journey and month, from **one average pool** in
battery kWh. The reasons are in [design.md](design.md) ("Price of a trip").
Every run recomputes it from the whole history, so a late receipt reprices
everything.

## Charges

- The kWh into the battery: homy's `energy_in_kwh` of the charge.
- **Home**: the money is `cost_eur` of the `ioniq_charges` row with
  `place = home` and the same start time. Months without a cost there (older
  prices) count as unknown.
- **Away**: the money is the receipt or the owner's answer from the store
  ([charges.md](charges.md)). An estimate is not money paid: until a
  receipt or an answer comes, the charge is unknown.

## The pool

- At each charge the pool is first set to SoC at the start × **kWh per %**.
  The pool keeps its known/unknown split and its price. What the pool lost
  since the last charge is the **standby drain** of that month. It also
  holds the errors of the trip kWh. When the SoC shows more than the pool,
  the extra goes in as unknown kWh.
- kWh per % is the median of battery kWh ÷ SoC change over home charges of
  20 % or more. Until there is one: 0.30.
- Then the charge goes in: known kWh and its money, or unknown kWh.
- A trip takes its net battery kWh (homy's `energy_net_kwh`: out less regen)
  at the pool's price. The price is money ÷ known kWh. The trip takes known
  and unknown kWh in their shares. Regen just stays in the pool.
- A trip gets a price only when the pool's **unknown share is 10 % or
  less**. Above 0 it is marked `price_estimated`.
- Not done yet: morning preheat while plugged in. #8 can tell it apart
  (a short block after the charge, [home-charge.md](home-charge.md)), but
  its kWh are not reported per morning yet.

## Outputs

- `ev_trips`: `cost_eur`, `price_eur_per_kwh`, `price_estimated`,
  `unknown_share`.
- `ev_journeys`: `cost_eur` (the sum of its trips; none when a trip has no
  price), `price_estimated`.
- `ev_month`: `spent_eur` (charges with a known cost), `driving_cost_eur`
  (the priced trips), `drain_kwh`, `eur_per_100km` (driving cost ÷ the km of
  the priced trips), `kwh_per_100km` (all trips).
- `ev_month`: also `eur_per_kwh`, `eur_per_kwh_home`, `eur_per_kwh_away`
  ([Overview](#overview)).
- MQTT `month`: `spent_eur`, `driving_cost_eur`, `eur_per_100km`,
  `kwh_per_100km`, `eur_per_kwh`, `eur_per_kwh_home`, `eur_per_kwh_away`.
  MQTT `overview`: [Overview](#overview). Each journey: `cost_eur`.

## Overview

Cost per kWh and per 100 km, for all time, the last 30 days (by start time,
up to the run) and each month (`ev_month` and MQTT `month`). MQTT `overview`
holds `all` and `last_30d`.

- `eur_per_kwh`: money paid ÷ kWh into the battery, over the charges with a
  known cost. `eur_per_kwh_home` and `eur_per_kwh_away` split it: home is a
  cost from the house meter, away is a receipt or an answer.
- `eur_per_100km`: the cost of the priced trips ÷ their km × 100.
- `kwh_per_100km`: net battery kWh ÷ km × 100, over all trips. With the
  price per kWh, it tells price and consumption apart.
- `charges_no_cost`, `trips_no_price` (MQTT only): what was left out.

All periods use the same rules; a trip without km or kWh counts as 0 there.

Per 100 km is not split into home and away: a trip takes energy from the
pool, which mixes both.

**No data, so no number.** A value is `null` when its period has nothing
to count; nothing is guessed:

- Months without `cost_eur` in `ioniq_charges` (older home prices) have no
  home price per kWh. Their trips often have no price either, so per 100 km
  is missing or counts only some trips (`trips_no_price`).
- An away charge without a receipt or an answer is left out of the price
  per kWh (`charges_no_cost`).
- A month with no away charge has no away price.

Spent and driving cost differ by the change in what the battery holds, by
the drain, and by trips with no price.
