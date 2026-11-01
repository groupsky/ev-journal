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
  Weekends and holidays keep theirs. An extra type only changes the
  departure; for the forecast it is a workday.

Workdays form one pool. Weekends and holidays form the other ("off" days):
there are too few holidays to learn from alone.

## Forecast

- The **need** of a day is the battery kWh of all trips that start on it
  (local time). Journeys marked "not me" are left out. A day without trips
  counts as 0.
- From the days of the same pool in the 28 days before:
  **normal day** = p50, **bad day** = p90 + 2 kWh (linear interpolation; at
  least 3 days).
- Standby drain is left out. It is about 0.2 kWh a day, well under the
  margin, and the trips do not include it.

### Back-test

Each of the last 91 days is forecast only from the days before it.
`ev-journal dry-run` prints it; `ev_forecast` keeps it per day. On real data
to 2026-10-31 (91 days):

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
