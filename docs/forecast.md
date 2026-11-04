# Forecast for tomorrow and charging advice

From [#9](https://github.com/groupsky/ev-journal/issues/9). Every run makes
the forecast for the next morning and says how to charge for it. The goal is
**battery life first**, then cost.

## The next morning

Tomorrow in the evening, today after midnight: the local date 12 h from now.
The pool takes the days up to the one before that morning. So in the
evening today's trips count, and the evening forecast is the same as the
back-test's forecast for that day. A run during the day counts today's trips
so far; only the evening one matters.

## Day types

- `weekend`: Saturday and Sunday.
- `holiday`: an official holiday of `EVJ_HOLIDAYS` (a country code of the
  [holidays](https://pypi.org/project/holidays/) package) or a date in
  `EVJ_DAYS_OFF`, on Monday to Friday.
- `workday`: the rest.
- Extra types: `EVJ_DAY_TYPES` names date ranges, for example
  `alt:2026-01-05..2026-01-09`. A workday inside a range gets that name.
  Weekends and holidays keep theirs, so a range that covers a weekend
  changes only its workdays (weekend shifts are not covered yet). An extra
  type sets the departure and has its own pool (below).

There are two kinds of day. Workdays (extra types too) are one kind.
Weekends and holidays are the other ("off" days): there are too few
holidays to learn from alone.

## Forecast

- The **need** of a day is the battery kWh of all trips that start on it
  (local time). Journeys marked "not me" are left out. A day without trips
  counts as 0.
- From the days of the same kind in the 28 days before:
  **normal day** = p50, **bad day** = p90 + 2 kWh (linear interpolation; at
  least 3 days). This is the base rule ("no key").
- **Calendar keys** ([#23](https://github.com/groupsky/ev-journal/issues/23))
  can narrow the pool to past days with the same value. Each key looks back
  further, so it still finds enough days:

  | key | value | looks back |
  |---|---|---|
  | `weekday` | Monday … Sunday | 84 days |
  | `iso_week_parity` | odd or even ISO week | 56 days |
  | `month_parity` | odd or even month | 122 days |
  | `month_part` | days 1–10, 11–20, 21–31 | 91 days |
  | `month` | the month of the year | 365 days |
  | `day_type` | workday, weekend, holiday or an extra type | 365 days |

  A pair of keys looks back as far as its longer key. The keys are in
  `calendar_keys.py`, apart from the pool code, so a later forecast can
  reuse them.
- **The back-test picks the keys**, for each kind on its own, at every
  run. It tries no key and each key alone. A choice *beats* no key when its
  bad-day coverage is not lower and its normal-day error is lower; all are
  scored on the days that no key scores. The pick is the single key with
  the lowest error among those that beat no key. When none does, it tries
  the pair of the two single keys with the lowest error, and picks it if it
  beats no key. Else no key. So a key is used only when it beats the base
  rule on the real data.
- **Fallback.** A pool with fewer than 3 days falls back to the next
  simpler choice: a pair to its first key, then to no key.
- **Extra types.** A day of an extra type (`EVJ_DAY_TYPES`) first pools from
  past days of the same name in the last 365 days. Fill in past periods too,
  so it has days to learn from. With fewer than 3 such days it falls back
  to the picked choice of workdays. This is not part of the pick: a named
  range is the owner's statement that those days differ. In the back-test,
  every choice (no key too) pools named days this way.
- Standby drain is left out. It is about 0.2 kWh a day, well under the
  margin, and the trips do not include it.

### Rhythms: effects

[#25](https://github.com/groupsky/ev-journal/issues/25). The pools above
split the days, so each pool is small. To find any regular rise or drop with
no manual input, the forecast also learns **effects** on top of the pool's
forecast ("today's rule").

- **Key families** (`calendar_keys.FAMILIES`), generated, each a small
  function: `weekday`; `day_of_month` (1–31); `day_from_month_end` (1 is the
  last day); `nth_weekday` (for example the first Tuesday);
  `week_mod2/3/4` and `month_mod2/3/4` (every 2nd, 3rd, 4th week or month,
  with each phase); `month`; `day_type`. Weeks count from a fixed Monday and
  months over the years, so a rhythm goes on over the new year.
- **Effect.** For each kind of day (workday or off) and each value of a
  family: the median of the residuals (real − today's rule) of the past
  days with that value, shrunk toward 0 by n / (n + 10). A value with fewer
  than 8 days, or whose median is less than 2 standard errors from 0, has no
  effect. The normal day and the bad day both get the effects added (never
  below 0).
- **Walk forward.** The days are taken in date order. A day gets the effects
  learned from the days before it, then it is learned. So every scored day
  gets its effects only from days before it. In the evening, today is
  learned too, with the trips so far, as in today's pool. Two things are
  not out of sample: the keys of today's rule are picked on the last 91
  days, and the families are picked on the same 365 days they are scored
  on. The guards keep a family picked by chance unlikely.
- **Pick.** Forward selection at every run, scored on the last 365 days:
  add the family that lowers the normal-day error most, while it beats the
  model before it (bad-day coverage not lower, normal-day error lower by at
  least 1.5 %); stop when none does, or at 3 families. A later family
  learns what is left after the ones before it. With none picked, today's
  rule is used unchanged. So the effects are used only while they win on
  the real data, and turn off again by themselves.
- These are fixed numbers in `forecast.py`, not settings. On made-up noise
  they pick nothing; a planted +4 kWh on every 5th is found.
- Named ranges (`EVJ_DAY_TYPES`) stay for changes with no rhythm.

`ev-journal dry-run` prints each step of the pick (keys, days, bad-day
coverage, normal-day error) and the effects of the used keys with their
count, for example `month_mod2 = 1 (workday): +1.8 kWh (n = 160)`. The
`advice` output lists the effects for that day.

On real data to 2026-11-03 there are about 4 months of trips. No family
wins yet, so today's rule is used: a monthly rhythm needs at least 8 past
days of the same value.

### Back-test

Each of the last 91 days is forecast only from the days before it, with
the picked keys and the effects. `ev-journal dry-run` prints it, and then
each tried choice per kind (days, bad-day coverage, normal-day error) and
which one was picked. `ev_forecast` keeps it per day. The keys are picked
on the same 91 days they are scored on; the effects families on the last
365 days. The table below is from before the keys. On real data to
2026-10-31 (91 days):

| N days | quantile | margin | bad day covers | mean over-forecast |
|---|---|---|---|---|
| 7 | p90 | +2 kWh | 92.3 % | 7.5 kWh |
| 14 | p90 | +1 kWh | 89.0 % | 6.8 kWh |
| 28 | p90 | 0 | 87.9 % | 4.6 kWh |
| 28 | p90 | +1 kWh | 89.0 % | 5.4 kWh |
| **28** | **p90** | **+2 kWh** | **92.3 %** | **6.3 kWh** |
| 28 | p95 | 0 | 92.3 % | 10.3 kWh |

The goal was 90 %. Workdays: 93.7 %; off days: 88.5–89.3 %. 4 of the 7
missed days were long trips over 20 kWh; planned trips are the input that
would catch those.

Left out for now:

- **Temperature.** Wh/km did not change with the morning temperature in the
  data so far (6–27 °C). It needs cold mornings to learn from. The
  Open-Meteo forecast is already in InfluxDB (`"weather"."weather"`,
  `kind = forecast`).
- **Planned trips** (a route or a place for tomorrow, by voice or from a
  calendar).

## Advice

- **Target SoC at departure** = bad day ÷ kWh per % + `EVJ_RESERVE_PCT`
  (10), rounded up to 5 %, at most 100 %. kWh per % comes from the home
  charges ([home-charge.md](home-charge.md)).
- **SoC now**: the live home charge estimate, or else the car's last SoC.
- At or above the target: no charge tonight.
- **Night tariff zone**: 22:00–06:00 from 1 November to 31 March,
  23:00–07:00 from 1 April to 31 October, by the date of the evening.
- **The charge ends** at the end of the zone, or at the departure when that
  falls inside the zone. The charge starts no earlier than now. Times are
  summed in real time, so the nights the clocks change are right.
- **Departure**: `EVJ_DEPARTURE_<TYPE>` for the day type of the morning
  (`HH:MM`, or `none` for the zone end), else `EVJ_DEPARTURE`, else none.
  Bad values (a bad time, an unknown type, a range named like a built-in
  type, a timer step of 0) stop the start-up, so they are found at once.
- **Level**: the lowest of the car's three AC levels that reaches the target
  in time. Battery side 0.92 / 1.60 / 2.34 kW (low / mid / high; 1.22 /
  1.93 / 2.64 kW at the house meter, 661 sessions). The time is rounded up to
  the step of the car's charge timer (`EVJ_TIMER_STEP_MIN`, 15 min). The
  start is the end less that time.
- When even the high level cannot reach the target inside the zone, the
  advice is high for the whole zone, and the target is what that reaches
  (`capped`).
- At these powers (0.04–0.1 C) the level hardly changes wear; time at a high
  % and heat do. So the charge goes only to the target and ends as late as
  the zone allows. Off days have their own, usually lower, need.
- **Preheat**: with `EVJ_PREHEAT=1` (a weekday preheat is set in the car), a
  holiday adds "turn the preheat off". The car has no modem, so this is a
  message.

## Delivery

`advice` in the MQTT summary and `GET /advice`
([outputs.md](outputs.md#mqtt), [api.md](api.md#advice)). The `message` is
the evening text in Bulgarian, for example:

> Утре: около 7 kWh (лош ден 15). Сега 40 %. Нужни 60 % при тръгване.
> Задай: ниско, 00:30–06:00.

or "… Не е нужно зареждане тази вечер." Sending it (Telegram or a Home
Assistant notification) is done outside this repo.
