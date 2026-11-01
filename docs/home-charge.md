# Home charge: SoC estimate and battery model drift

From [#8](https://github.com/groupsky/ev-journal/issues/8). During a home
charge the car sleeps, so nothing reads its SoC. The house charger meter
does run. This page says how the SoC is estimated from it, how well that
works, and how the battery model is watched for drift.

## Inputs

- The house charger meter: `xymd1` with tag `device.name = charger`. Field
  `ap` is the power (W), `tot_act` the meter total (kWh, 0.01 steps). Read
  per minute: `max(ap)` and `last(tot_act)`. (`pow` is the power factor,
  not the power.)
- The car: `ioniq`, `group = bms/2101`, field `soc`: the last reading
  before the charge starts.
- The finished charges, for learning: `ioniq_sessions` (`kind = charge`:
  `energy_in_kwh` = battery kWh, `soc_start`, `soc_end`) and the
  `ioniq_charges` rows with `place = home` (same start time).
- The charge row in `ioniq_sessions` is written only when the car moves
  again (or the bot restarts), not when the charge ends. So the live
  estimate works from the meter alone.

## Meter kWh while charging (E)

- A minute with `ap` above 150 W is charging. A pause of up to 2 min stays
  inside one block. This is the rule homy's `ioniq-sessions` uses. At home
  the power is always one of the car's three AC levels, so any limit from
  150 W to 1000 W gives the same blocks.
- The energy of a block is the change of `tot_act` from the minute before
  it to its last minute. A gap in the power data then loses nothing.
- **Preheat**: a block after the first one that lasts 40 min or less. It is
  left out of E. Live, a later block is left out until it is longer than
  40 min, because the start of a preheat looks like charging.

## The model

```
SoC now = SoC at plug-in + max(0, E − overhead) × ratio ÷ kWh per %
```

- `ratio` and `overhead`: a least-squares line of battery kWh against E over
  the past home charges. Slope = ratio; overhead = −intercept ÷ slope.
- `kWh per %`: the median of battery kWh ÷ SoC change over home charges of
  20 % or more (as in [price.md](price.md)).
- Until there are two charges: ratio 0.90, overhead 1.0 kWh, 0.30 kWh per %.
- Meter kWh to the target = (target − SoC now) × kWh per % ÷ ratio. Time
  to the target uses the power now. The target is `EVJ_SOC_TARGET` until
  the charging advice (#9) sets it.

On all home charges to 2026-10-31 (82): ratio 0.903, overhead 1.08 meter
kWh, 0.305 kWh per %.

## Back-test

Walk-forward: each charge is scored with a model learned only from the
charges before it, after the first 10. `ev-journal dry-run` prints it.

| charges scored | median error | p90 | max |
|---|---|---|---|
| 72 | 1.49 % SoC | 3.24 % SoC | 5.9 % SoC |

The goal was a median under 2 % and a p90 under 4 %. The worst errors are
the one charge at the middle power level and some cold charges in 2026-10.
Two extras were tried and left out because the simple model meets the goal:
the outside temperature (1.31 % / 2.92 %), and a separate ratio for charges
up to 4 kWh (1.25 % / 2.92 %).

## Where the fixed ~2.7 kWh go

The issue's fit was meter kWh = 2.66 + 0.293 × ΔSoC.

- **Mostly the sample.** That exact fit comes back only when charges under
  10 % ΔSoC are left out (72 charges). With all 82 the fit is
  meter kWh = 1.60 + 0.323 × ΔSoC. The issue's slope below the battery
  slope was the warning sign.
- **Ruled out, 0.00 kWh each:** standby or low power inside the session,
  start ramps, end taper, meter energy just before or after the session,
  and time plugged in but not charging. The rest of the fit does not grow
  with charging time either (r = 0.00).
- **Preheat while plugged:** 7 of 82 charges, each block 30 min and about
  0.61 kWh, always after the main charge. That is 0.05 kWh per charge on
  average. Without it the intercept falls from 1.60 to 1.42 kWh.
- **The battery side:** battery kWh = 0.14 + 0.302 × ΔSoC, about 0.17 meter
  kWh of the intercept.
- **Left, about 1.0–1.25 kWh:** the meter-to-battery ratio grows with the
  size of the charge. The marginal ratio is 0.90 while the average is 0.81,
  and charges up to 4 kWh have a ratio of about 0.6–0.67. The data with the
  car awake while charging shows a ratio of about 0.75 from the first
  minutes, so it is not a start ramp. Most likely the car draws extra power
  while it is awake. The data cannot prove it.
- The ratio is lower in the cold: 0.76 at 15 °C or below, 0.80–0.81 above
  20 °C (charges of 9–13 kWh).
- Four meter blocks have no charge row in `ioniq_sessions`, so they are not
  learned from.

## Drift

Per month, from the home charges that start in it:

- `kwh_per_pct`: as above, over the month's charges of 20 % or more.
- `meter_ratio`: battery kWh ÷ (E − overhead), summed over the month, with
  the overhead of all charges. Without the overhead, a month with more small
  charges would show a lower ratio while the battery did not change.
- `overhead_kwh`: the median of E − battery kWh ÷ ratio, with the ratio of
  all charges.

Each is compared with the median of the up to 3 months before it. Moves
over `EVJ_DRIFT_PCT` (3 %) for `kwh_per_pct` and `meter_ratio`, and over
`EVJ_DRIFT_KWH` (0.5 kWh) for `overhead_kwh`, go into `drift_flag`. The
overhead moves 11–27 % from month to month, so a limit in % does not work
for it.

| month | charges | kWh per % | ratio | overhead (kWh) | flag |
|---|---|---|---|---|---|
| 2026-07 | 10 | 0.303 | 0.898 | 1.18 | – |
| 2026-08 | 21 | 0.304 | 0.934 | 0.87 | `meter_ratio +4.0 %` |
| 2026-09 | 23 | 0.304 | 0.902 | 1.15 | – |
| 2026-10 | 28 | 0.309 | 0.881 | 1.29 | – |

The ratio and the overhead of a month move together: 2026-08 has the lowest
overhead and so the highest ratio. With only one month before it, its
median is that one month. Battery kWh ÷ E without the overhead (0.810,
0.831, 0.815, 0.788) flags 2026-10 instead (−3.4 %), the coldest month
(median 13.5 °C). A year of data will tell which limit is right.
