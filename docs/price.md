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
- MQTT `month`: `spent_eur`, `driving_cost_eur`, `eur_per_100km`,
  `kwh_per_100km`. Each journey: `cost_eur`.

Spent and driving cost differ by the change in what the battery holds, by
the drain, and by trips with no price.
