# Away charges

For each charge that is not at home: which station it was, and what it cost
per battery kWh. #5 uses the price.

## Stations

- The Fines Charging public API (no key): `GET
  https://public.finescharging.com/v1/locations.geojson`. One GeoJSON point
  per location (`[lon, lat]`), with `name`, `connectors`, `max_power_kw` and
  `price_range` (`min`, `max`, EUR per kWh). The range is per location, not
  per connector.
- Fetched **at most once a day** into `EVJ_DATA_DIR/fines-locations.geojson`
  and the `stations` table. A file that fails to download or to parse is
  not kept; the last good copy stays in use. After a failed download the
  next try waits 1 h.
- Other networks later.

## Match

- The car's position at the charge: the real GPS fix nearest in time, either
  the last one before the start or the first one after the end, at most
  30 min away. No such fix: `no position`.
- A charge is at home when that position is in a `home` place. When the
  position is in no place (or unknown), the place where the trip before it
  ended decides. Home charges are not matched (`home`) and not priced here.
- A charge that is gone from homy is deleted, with its open question.
- The station is the nearest one within **75 m**: `ok`. Two or more within
  75 m: the nearest, marked `unsure`. None: `no station`.
- The `charges` table keeps the station id. The name comes from the feed.

## Price and network kWh

Best first:

1. **Receipt**: `POST /charges/{id}/receipt` with `amount_eur`,
   `network_kwh` and the receipt `time`. The id is the charge start (ms).
2. **Answer**: a question opens for an away charge with no receipt, 24 h
   after it ends. Only charges that ended in the last 14 days get one, so the
   first run does not ask about the whole history. `GET /questions` lists
   the open ones (date, station name from the feed, the car's kWh).
   `POST /questions/{id}/answer` with `{"text": "5.00 12.5"}`: the amount
   in EUR, then the network's kWh. A comma also works as the decimal mark.
3. **Estimate**: the feed's price × the estimated network kWh. AC charges
   use the low end of the site's range, DC and unknown the high end. The
   network kWh = the car's kWh × the loss ratio: the median of network ÷
   battery kWh over all receipts and answers, or 1.10 until there is one.

The `charges` table then has `price_eur_per_kwh` (EUR per **battery** kWh),
`price_source` (`receipt`, `answer`, `estimate`), `network_kwh` and
`cost_eur`. A receipt or an answer replaces an estimate at once.

Network kWh ÷ battery kWh is the charging loss at that station.

## Price estimate during a charge

The car's app shows a cost while it charges. It asks here and does not copy
the rule: `GET /charges/price-estimate?lat=<>&lon=<>&kind=dc|ac` (`kind` is
required). The answer:

- `station`: `id`, `name` and `max_power_kw` of the nearest station within
  75 m, or `null`. `match`: `ok`, `unsure` (two or more within 75 m) or
  `no station`, the same rule as above.
- `eur_per_battery_kwh` and its `source`, best first:
  1. `last_receipt`: the price of the latest charge at this station with a
     receipt or an answer.
  2. `feed`: the feed's price × the loss ratio. DC uses the high end of the
     site's range, AC the low end.
  Both are `null` when there is no station, or the station has no price.
- `loss_ratio`: the median of network ÷ battery kWh over receipts and
  answers, or 1.10.

The app keeps the last answer for when the internet drops.
