import json
import os
from zoneinfo import ZoneInfo

import pytest
from week import PLACES, week

from ev_journal import charges, places
from ev_journal.charges import Station, match
from ev_journal.journeys import Track, build

UTC = ZoneInfo("UTC")
HOUR = 3_600_000
DAY = 24 * HOUR

# Made-up stations in the test area. [lon, lat] as in GeoJSON.
FEED = {
    "type": "FeatureCollection",
    "features": [
        {
            "type": "Feature",
            "id": 1,
            "geometry": {"type": "Point", "coordinates": [0.5, 0.5]},
            "properties": {
                "name": "Test DC",
                "connectors": {"CCS": 1},
                "max_power_kw": 50,
                "price_range": {"min": 0.40, "max": 0.50},
            },
        },
        {
            "type": "Feature",
            "id": 2,
            "geometry": {"type": "Point", "coordinates": [-0.2, 0.7]},
            "properties": {
                "name": "Test Lake A",
                "connectors": {"Type 2": 2},
                "max_power_kw": 22,
                "price_range": {"min": 0.30, "max": 0.30},
            },
        },
        {
            "type": "Feature",
            "id": 3,
            "geometry": {"type": "Point", "coordinates": [-0.2, 0.7004]},
            "properties": {
                "name": "Test Lake B",
                "connectors": {"Type 2": 1},
                "max_power_kw": 11,
                "price_range": {"min": 0.35, "max": 0.35},
            },
        },
    ],
}
RAW = json.dumps(FEED).encode()


def test_match_one_two_none():
    a = Station("a", "A", 0.5, 0.5, None, None)
    b = Station("b", "B", 0.5004, 0.5, None, None)  # about 45 m north of a
    assert match((0.5, 0.5001), [a]) == ("a", "ok")
    assert match((0.5001, 0.5), [a, b]) == ("a", "unsure")
    assert match((0.5003, 0.5), [a, b]) == ("b", "unsure")
    assert match((0.51, 0.5), [a, b]) == (None, "no station")
    assert match(None, [a]) == (None, "no position")


def test_position_prefers_the_fix_before_the_start():
    t = Track([(0, 0.1, 0.1), (100 * 60_000, 0.2, 0.2)])
    # 15 min before the start wins over 10 min after the end (#28).
    assert charges.position(t, 15 * 60_000, 90 * 60_000) == (0.1, 0.1)
    assert charges.position(t, 5 * 60_000, 80 * 60_000) == (0.1, 0.1)
    # No fix before the start within 30 min: the first after the end.
    assert charges.position(t, 40 * 60_000, 90 * 60_000) == (0.2, 0.2)
    assert charges.position(Track([(0, 0.1, 0.1)]), 31 * 60_000, 40 * 60_000) is None


class Feed:
    def __init__(self, raw=RAW):
        self.raw, self.calls = raw, 0

    def __call__(self, url):
        self.calls += 1
        if isinstance(self.raw, Exception):
            raise self.raw
        return self.raw


def _age(path, ms):
    t = path.stat().st_mtime - ms / 1000
    os.utime(path, (t, t))


def test_feed_is_fetched_daily_and_the_last_good_copy_kept(tmp_path):
    db = places.open_store(tmp_path)
    cache = tmp_path / "fines.geojson"
    now = int(cache.parent.stat().st_mtime * 1000)
    assert charges.update_stations(db, cache, now, Feed(OSError("down"))).startswith("failed")
    assert charges.all_stations(db) == []
    feed = Feed()
    now += 2 * HOUR  # after the wait that follows a failed try
    assert charges.update_stations(db, cache, now, feed) == "fetched"
    assert charges.update_stations(db, cache, now, feed) == "cached"
    assert feed.calls == 1
    assert {s.id for s in charges.all_stations(db)} == {"fines:1", "fines:2", "fines:3"}
    # A day later the API fails, or sends junk: the last good copy stays.
    for bad in (OSError("down"), b'{"type": "nope"}'):
        _age(cache, DAY + 1)
        cache.with_suffix(".tried").unlink()  # the 1 h wait is over
        status = charges.update_stations(
            db, cache, int(cache.stat().st_mtime * 1000) + DAY + 2, Feed(bad)
        )
        assert status.startswith("failed")
        assert cache.read_bytes() == RAW
        assert len(charges.all_stations(db)) == 3


@pytest.fixture
def store(tmp_path):
    db = places.open_store(tmp_path)
    cache = tmp_path / "fines.geojson"
    charges.update_stations(db, cache, 0, Feed())
    w = week()
    r = build(w.trips, w.charges, w.fixes, PLACES, UTC)
    end = max(c.end for c in r.charges)
    charges.sync(db, r, PLACES, end + 2 * DAY)
    return db, r


def _row(db, cid):
    db.row_factory = None
    return db.execute(
        "SELECT at_home, station_id, match, price_source, price_eur_per_kwh, network_kwh"
        " FROM charges WHERE id = ?",
        (cid,),
    ).fetchone()


def test_sync_matches_and_estimates(store):
    db, r = store
    home, dc, lake, home2 = sorted(r.charges, key=lambda c: c.start)
    assert _row(db, home.start)[:4] == (1, None, "home", None)
    at_home, sid, how, src, per_kwh, net = _row(db, dc.start)
    assert (at_home, sid, how, src) == (0, "fines:1", "ok", "estimate")
    # DC uses the high end of the range; the network kWh uses the default loss.
    assert net == pytest.approx(30.0 * charges.DEFAULT_LOSS)
    assert per_kwh == pytest.approx(0.50 * charges.DEFAULT_LOSS)
    assert _row(db, lake.start)[1:3] == ("fines:2", "unsure")


def test_receipt_replaces_estimate_and_teaches_the_loss(store):
    db, r = store
    dc = sorted(r.charges, key=lambda c: c.start)[1]
    db.execute("INSERT INTO receipts VALUES (?, 16.5, 33.0, NULL, 0)", (dc.start,))
    charges.price(db, dc.start)
    src, per_kwh, net = _row(db, dc.start)[3:]
    assert src == "receipt" and net == 33.0
    assert per_kwh == pytest.approx(16.5 / 30.0)
    assert charges.loss_ratio(db) == pytest.approx(33.0 / 30.0)


def test_questions_open_after_24_h(tmp_path):
    db = places.open_store(tmp_path)
    charges.update_stations(db, tmp_path / "f.geojson", 0, Feed())
    w = week()
    r = build(w.trips, w.charges, w.fixes, PLACES, UTC)
    last_away = sorted(r.charges, key=lambda c: c.start)[2]  # the lake charge
    charges.sync(db, r, PLACES, last_away.end + DAY - 1)
    opened = {q[0] for q in db.execute("SELECT charge_id FROM questions")}
    assert last_away.start not in opened
    charges.sync(db, r, PLACES, last_away.end + DAY)
    opened = {q[0] for q in db.execute("SELECT charge_id FROM questions")}
    assert last_away.start in opened
    assert not any(c.start in opened for c in r.charges if c.place and c.place.kind == "home")


def test_ac_uses_the_low_end_and_resync_keeps_answers(store):
    db, r = store
    dc, lake = sorted(r.charges, key=lambda c: c.start)[1:3]
    db.execute("UPDATE charges SET type = 'AC' WHERE id = ?", (dc.start,))
    charges.price(db, dc.start)
    assert _row(db, dc.start)[4] == pytest.approx(0.40 * charges.DEFAULT_LOSS)
    db.execute(
        "UPDATE questions SET answered_at = 1, amount_eur = 3.0, network_kwh = 11.0"
        " WHERE charge_id = ?",
        (lake.start,),
    )
    db.execute("INSERT INTO receipts VALUES (?, 16.5, 33.0, NULL, 0)", (dc.start,))
    end = max(c.end for c in r.charges)
    charges.sync(db, r, PLACES, end + 2 * DAY)  # the next run
    assert _row(db, dc.start)[3] == "receipt"
    assert _row(db, lake.start)[3] == "answer"
    # One value per charge in the loss ratio; the median of two is their mean.
    assert charges.loss_ratio(db) == pytest.approx((33.0 / 30.0 + 11.0 / 10.0) / 2)


def test_gone_and_now_home_charges_lose_price_and_question(store):
    db, r = store
    dc, lake = sorted(r.charges, key=lambda c: c.start)[1:3]
    assert {q[0] for q in db.execute("SELECT charge_id FROM questions")} == {dc.start, lake.start}
    # The lake becomes a home place, and the DC charge is gone from homy.
    lake_home = places.Place("p-lake", "Test Lake", "home", 0.7, -0.2, 150)
    rest = [p for p in PLACES if p.id != "p-lake"] + [lake_home]
    r.charges = [c for c in r.charges if c is not dc]
    charges.sync(db, r, rest, max(c.end for c in r.charges) + 2 * DAY)
    assert db.execute("SELECT 1 FROM charges WHERE id = ?", (dc.start,)).fetchone() is None
    assert _row(db, lake.start)[0] == 1 and _row(db, lake.start)[3] is None
    assert db.execute("SELECT count(*) FROM questions").fetchone()[0] == 0


def test_no_questions_for_old_charges(tmp_path):
    db = places.open_store(tmp_path)
    charges.update_stations(db, tmp_path / "f.geojson", 0, Feed())
    w = week()
    r = build(w.trips, w.charges, w.fixes, PLACES, UTC)
    charges.sync(db, r, PLACES, max(c.end for c in r.charges) + 15 * DAY)
    assert db.execute("SELECT count(*) FROM questions").fetchone()[0] == 0


def test_a_failed_fetch_waits_before_the_next_try(tmp_path):
    db = places.open_store(tmp_path)
    cache = tmp_path / "f.geojson"
    down = Feed(OSError("down"))
    now = int(tmp_path.stat().st_mtime * 1000) + 10_000
    charges.update_stations(db, cache, now, down)
    charges.update_stations(db, cache, now + 60_000, down)
    assert down.calls == 1
    assert charges.update_stations(db, cache, now + 2 * HOUR, Feed()) == "fetched"


def test_parse_answer():
    assert charges.parse_answer("5.00 12.5") == (5.0, 12.5)
    assert charges.parse_answer("5,00  12,5 ") == (5.0, 12.5)
    for bad in ("5.00", "a b", "0 3", "1 2 3"):
        with pytest.raises(ValueError):
            charges.parse_answer(bad)


def test_question_answered_once_through_the_api(client, data_dir):
    db = places.open_store(data_dir)
    charges.update_stations(db, data_dir / "f.geojson", 0, Feed())
    w = week()
    r = build(w.trips, w.charges, w.fixes, PLACES, UTC)
    charges.sync(db, r, PLACES, max(c.end for c in r.charges) + 2 * DAY)
    qs = client.get("/questions").json()
    assert len(qs) == 2  # the DC charge and the lake charge
    q = qs[0]
    assert q["station_name"] == "Test DC" and q["battery_kwh"] == 30.0 and q["date"] == "2026-01-07"
    assert _row(db, q["id"])[3] == "estimate"
    assert client.post(f"/questions/{q['id']}/answer", json={"text": "one"}).status_code == 422
    got = client.post(f"/questions/{q['id']}/answer", json={"text": "15.00 33.0"}).json()
    assert got["price_source"] == "answer"
    assert got["price_eur_per_kwh"] == pytest.approx(0.5)
    assert _row(db, q["id"])[3] == "answer"
    assert [x["id"] for x in client.get("/questions").json()] == [qs[1]["id"]]
    # A receipt then wins over the answer.
    got = client.post(
        f"/charges/{q['id']}/receipt", json={"amount_eur": 14.0, "network_kwh": 32.0}
    ).json()
    assert got["price_source"] == "receipt"
    assert (
        client.post("/charges/1/receipt", json={"amount_eur": 1, "network_kwh": 1}).status_code
        == 404
    )
    assert client.post("/questions/1/answer", json={"text": "1 1"}).status_code == 404


def test_price_estimate_at_a_position(client, data_dir):
    db = places.open_store(data_dir)
    charges.update_stations(db, data_dir / "f.geojson", 0, Feed())
    w = week()
    r = build(w.trips, w.charges, w.fixes, PLACES, UTC)
    charges.sync(db, r, PLACES, max(c.end for c in r.charges) + 2 * DAY)

    def ask(lat, lon, kind):
        return client.get(
            "/charges/price-estimate", params={"lat": lat, "lon": lon, "kind": kind}
        ).json()

    # Only the feed price: DC is the high end × the loss ratio.
    got = ask(0.5001, 0.5, "dc")
    assert got["station"] == {"id": "fines:1", "name": "Test DC", "max_power_kw": 50}
    assert (got["match"], got["source"]) == ("ok", "feed")
    assert got["eur_per_battery_kwh"] == pytest.approx(0.50 * charges.DEFAULT_LOSS)
    assert got["loss_ratio"] == charges.DEFAULT_LOSS
    # Two stations within 75 m: the nearest, unsure. AC is the low end.
    got = ask(0.7, -0.2, "ac")
    assert (got["station"]["id"], got["match"], got["source"]) == ("fines:2", "unsure", "feed")
    assert got["eur_per_battery_kwh"] == pytest.approx(0.30 * charges.DEFAULT_LOSS)
    # No station within 75 m.
    got = ask(0.1, 0.1, "dc")
    assert got == {
        "station": None,
        "match": "no station",
        "eur_per_battery_kwh": None,
        "source": None,
        "loss_ratio": charges.DEFAULT_LOSS,
    }
    # A receipt at the station: its price wins, and it teaches the loss ratio.
    dc = sorted(r.charges, key=lambda c: c.start)[1]
    client.post(f"/charges/{dc.start}/receipt", json={"amount_eur": 16.5, "network_kwh": 36.0})
    got = ask(0.5, 0.5, "dc")
    assert got["source"] == "last_receipt"
    assert got["eur_per_battery_kwh"] == pytest.approx(16.5 / 30.0)
    assert got["loss_ratio"] == pytest.approx(36.0 / 30.0)
    # The DC receipt does not price an AC charge there.
    got = ask(0.5, 0.5, "ac")
    assert (got["station"]["id"], got["source"]) == ("fines:1", "feed")
    assert got["eur_per_battery_kwh"] == pytest.approx(0.40 * 36.0 / 30.0)
    assert client.get("/charges/price-estimate", params={"lat": 0, "lon": 0}).status_code == 422


def test_meter_recorded_charge_is_home_even_with_a_far_fix(tmp_path):
    db = places.open_store(tmp_path)
    charges.update_stations(db, tmp_path / "f.geojson", 0, Feed())
    w = week()
    r = build(w.trips, w.charges, w.fixes, PLACES, UTC)
    dc = sorted(r.charges, key=lambda c: c.start)[1]  # at a station, far from home
    db.row_factory = None
    charges.sync(db, r, PLACES, dc.end + 2 * DAY)
    assert _row(db, dc.start)[0] == 0
    dc.home_cost = True  # the house charger meter recorded it
    charges.sync(db, r, PLACES, dc.end + 2 * DAY)
    assert _row(db, dc.start)[:3] == (1, None, "home")
    assert not db.execute(
        "SELECT 1 FROM questions WHERE charge_id = ? AND answered_at IS NULL", (dc.start,)
    ).fetchall()
