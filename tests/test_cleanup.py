"""A journey that is no longer produced leaves no row after the next run."""

import os
import re
from zoneinfo import ZoneInfo

import pytest
from week import PLACES, week

from ev_journal import cleanup, outputs
from ev_journal.influx import Influx
from ev_journal.journeys import build

UTC = ZoneInfo("UTC")


class FakeInflux:
    """Just enough of InfluxDB 1.x for the cleanup: rows keyed by series and time."""

    def __init__(self):
        self.rows: dict[str, dict[tuple[str, int], str]] = {}

    def write(self, lines):
        for ln in lines:
            head, _fields, t = ln.rsplit(" ", 2)
            m, _, tags = head.partition(",")
            self.rows.setdefault(m, {})[(tags, int(t))] = ln

    def query(self, q):
        if m := re.match(r'SELECT "\w+" FROM "(\w+)"', q):
            return [{"time": t} for (_tags, t) in self.rows.get(m[1], {})]
        if q.startswith("SHOW TAG VALUES"):
            tags = {tags for (tags, _t) in self.rows.get("ev_track", {})}
            return [{"key": "journey", "value": t.split("=")[1]} for t in tags]
        raise AssertionError(q)

    def execute(self, stmts):
        for s in stmts:
            if m := re.match(r'DELETE FROM "(\w+)" WHERE time = (\d+)ms', s):
                rows = self.rows[m[1]]
                for k in [k for k in rows if k[1] == int(m[2])]:
                    del rows[k]
            elif m := re.match(r"DROP SERIES FROM \"ev_track\" WHERE \"journey\" = '(\d+)'", s):
                rows = self.rows["ev_track"]
                for k in [k for k in rows if k[0] == f"journey={m[1]}"]:
                    del rows[k]
            else:
                raise AssertionError(s)


def two_results():
    """Two runs over the same week. In the first run, Tue's first trip ends
    35 min early (as if closed early), so the stop is 45 min and splits. In
    the second run the stop is the real 10 min: the two Tue journeys merge."""
    w1 = week()
    w1.trips[2].end -= 35 * 60_000
    late = w1.trips[3]
    first = build(w1.trips, w1.charges, w1.fixes, PLACES, UTC)
    w2 = week()
    second = build(w2.trips, w2.charges, w2.fixes, PLACES, UTC)
    assert len(first.journeys) == len(second.journeys) + 1
    stale = {j.id for j in first.journeys} - {j.id for j in second.journeys}
    assert stale == {late.start}
    return first, second, late.start


def _run(db, r):
    db.write(outputs.influx_lines(r))
    return cleanup.cleanup(db, db, r)


def test_merged_journey_leaves_no_row():
    first, second, stale_id = two_results()
    db = FakeInflux()
    _run(db, first)
    assert any(t == stale_id for (_tags, t) in db.rows["ev_journeys"])
    assert any(tags == f"journey={stale_id}" for (tags, _t) in db.rows["ev_track"])
    assert _run(db, second) > 0
    assert {t for (_tags, t) in db.rows["ev_journeys"]} == {j.id for j in second.journeys}
    assert {tags for (tags, _t) in db.rows["ev_track"]} == {
        f"journey={j.id}" for j in second.journeys
    }
    assert {t for (_tags, t) in db.rows["ev_trips"]} == {t.start for t in second.trips}


def test_rerun_deletes_nothing():
    _first, second, _ = two_results()
    db = FakeInflux()
    _run(db, second)
    assert _run(db, second) == 0


def test_empty_run_deletes_nothing():
    _first, second, _ = two_results()
    db = FakeInflux()
    _run(db, second)
    assert cleanup.cleanup(db, db, build([], [], [], PLACES, UTC)) == 0
    assert len(db.rows["ev_journeys"]) == len(second.journeys)


@pytest.mark.skipif(not os.environ.get("EVJ_TEST_INFLUX_URL"), reason="no test InfluxDB")
def test_against_real_influxdb():
    """Runs against a throwaway InfluxDB 1.x with a WRITE-only and a READ-only user.

    EVJ_TEST_INFLUX_URL, EVJ_TEST_INFLUX_DB, EVJ_TEST_WRITE (user:password),
    EVJ_TEST_READ (user:password).
    """
    url, db = os.environ["EVJ_TEST_INFLUX_URL"], os.environ["EVJ_TEST_INFLUX_DB"]
    writer = Influx(url, db, *os.environ["EVJ_TEST_WRITE"].split(":", 1))
    reader = Influx(url, db, *os.environ["EVJ_TEST_READ"].split(":", 1))
    first, second, stale_id = two_results()
    writer.write(outputs.influx_lines(first))
    cleanup.cleanup(reader, writer, first)
    writer.write(outputs.influx_lines(second))
    assert cleanup.cleanup(reader, writer, second) > 0
    times = {int(r["time"]) for r in reader.query('SELECT "end_ts" FROM "ev_journeys"')}
    assert times == {j.id for j in second.journeys}
    tracks = {
        r["value"] for r in reader.query('SHOW TAG VALUES FROM "ev_track" WITH KEY = "journey"')
    }
    assert str(stale_id) not in tracks
    assert cleanup.cleanup(reader, writer, second) == 0
