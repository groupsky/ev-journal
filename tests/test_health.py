"""Health series rules with made-up frames (#49)."""

from datetime import date, datetime
from zoneinfo import ZoneInfo

from conftest import TOKEN, CheckedClient

from ev_journal import places
from ev_journal.api import create_app
from ev_journal.health import Frames, Health, summarize

UTC = ZoneInfo("UTC")
H = 3_600_000


def ms(d: str, hour: float = 0) -> int:
    return int(datetime.fromisoformat(d).replace(tzinfo=UTC).timestamp() * 1000 + hour * H)


def bms(t: int, **kw) -> dict:
    return {"time": t, **kw}


def test_daily_rules():
    d = "2026-03-02"
    fr = Frames(
        soh=[(ms(d, 1), 97.1), (ms(d, 2), 97.4), (ms(d, 3), 0.0), (ms(d, 4), 255.0)],
        module_spread=[(ms(d, 1), 2.0), (ms(d, 2), 4.0), (ms(d, 3), 900.0)],
        bms=[
            bms(ms(d, 1), isolation_kohm=1000.0, cell_max_v=3.90, cell_min_v=3.88, hv_a=1.0),
            bms(ms(d, 2), isolation_kohm=800.0, cell_max_v=3.91, cell_min_v=3.88, hv_a=-2.0),
            bms(ms(d, 3), isolation_kohm=0.0, cell_max_v=3.92, cell_min_v=3.88, hv_a=3.0),
            # Under load: counts for the max, not for the median.
            bms(ms(d, 4), cell_max_v=3.95, cell_min_v=3.88, hv_a=-40.0),
            # Garbage: out of bounds, or max below min.
            bms(ms(d, 5), isolation_kohm=-5.0, cell_max_v=9.0, cell_min_v=3.0, hv_a=0.0),
            bms(ms(d, 6), cell_max_v=3.0, cell_min_v=3.5, hv_a=0.0),
        ],
    )
    row = summarize(fr, UTC)[d]
    assert row["soh_max"] == 97.4
    assert row["isolation_min_kohm"] == 800.0
    assert row["cell_spread_mv"] == 30.0  # median of 20, 30, 40
    assert row["cell_spread_mv_max"] == 70.0
    assert row["module_temp_spread_c_max"] == 4.0


def test_null_fields_on_a_day_with_only_garbage():
    d = "2026-03-02"
    fr = Frames(soh=[(ms(d, 1), 0.0)], bms=[bms(ms(d, 2), aux_12v=40.0, hv_a=0.0)])
    row = summarize(fr, UTC)[d]
    assert all(row[f] is None for f in row if f != "fan") and row["fan"] == {}
    assert summarize(Frames(), UTC) == {}


def test_aux_12v_rest_is_the_first_reading_after_a_6h_park():
    d = "2026-03-02"
    fr = Frames(
        last_aux_before=ms(d, -1),  # parked only 2 h before the first reading
        bms=[
            bms(ms(d, 1), aux_12v=14.2),
            bms(ms(d, 2), aux_12v=99.0),  # garbage: neither a reading nor a park end
            bms(ms(d, 6.5), aux_12v=14.1),  # 5.5 h after 01:00: too short
            bms(ms(d, 13), aux_12v=12.6),  # 6.5 h park: the rest reading
            bms(ms(d, 13.1), aux_12v=14.3),
            bms(ms(d, 20), aux_12v=12.5),  # a second long park the same day: first one wins
        ],
    )
    assert summarize(fr, UTC)[d]["aux_12v_rest"] == 12.6
    # No reading before the window counts as a long park.
    fr.last_aux_before = None
    assert summarize(fr, UTC)[d]["aux_12v_rest"] == 14.2


def test_days_split_in_local_time():
    tz = ZoneInfo("Etc/GMT-3")  # UTC+3
    fr = Frames(soh=[(ms("2026-03-01", 22), 97.0)])
    assert list(summarize(fr, tz)) == ["2026-03-02"]


def fan_frames(d: str, step: int, feedbacks: list[float]) -> list[dict]:
    return [bms(ms(d, 1) + i, fan_status=step, fan_feedback=f) for i, f in enumerate(feedbacks)]


def make(tmp_path, frames_by_day: dict[str, Frames], today: str):
    """A Health over made-up days; reads are logged as (first day, last day)."""
    reads = []

    def read(start: int, end: int) -> Frames:
        reads.append((start, end))
        out = Frames()
        for _d, fr in frames_by_day.items():
            out.soh += [x for x in fr.soh if start <= x[0] < end]
            out.bms += [x for x in fr.bms if start <= x["time"] < end]
        return out

    places.open_store(tmp_path).close()
    now = ms(today, 12) / 1000
    return Health(places.store_path(tmp_path), UTC, read, lambda: now), reads


def test_fan_table_per_month_and_step(tmp_path):
    days = {
        "2026-01-10": Frames(bms=fan_frames("2026-01-10", 3, [40, 42, 44])),
        "2026-01-20": Frames(
            bms=fan_frames("2026-01-20", 3, [38]) + fan_frames("2026-01-20", 5, [80])
        ),
        "2026-02-05": Frames(
            bms=fan_frames("2026-02-05", 3, [30, 31]) + fan_frames("2026-02-05", 0, [0])
        ),
    }
    h, _ = make(tmp_path, days, "2026-02-20")
    assert h.fan(2) == [
        {"month": "2026-01", "step": 3, "count": 4, "fan_feedback_median": 41.0},
        {"month": "2026-01", "step": 5, "count": 1, "fan_feedback_median": 80.0},
        {"month": "2026-02", "step": 3, "count": 2, "fan_feedback_median": 30.5},
    ]
    assert [r["month"] for r in h.fan(1)] == ["2026-02"]


def test_cached_past_days_are_not_read_again(tmp_path):
    days = {
        d: Frames(soh=[(ms(d, 10), 97.0)])
        for d in ("2026-01-30", "2026-02-02", "2026-02-19", "2026-02-20")
    }
    h, reads = make(tmp_path, days, "2026-02-20")
    first = h.daily(date(2026, 1, 25), date(2026, 2, 20))
    assert [r["day"] for r in first] == ["2026-01-30", "2026-02-02", "2026-02-19", "2026-02-20"]
    # One read per month for the past, one for yesterday and today.
    assert reads == [
        (ms("2026-01-25"), ms("2026-02-01")),
        (ms("2026-02-01"), ms("2026-02-19")),
        (ms("2026-02-19"), ms("2026-02-21")),
    ]
    reads.clear()
    assert h.daily(date(2026, 1, 25), date(2026, 2, 20)) == first
    assert reads == [(ms("2026-02-19"), ms("2026-02-21"))]  # only yesterday and today
    reads.clear()
    assert h.daily(date(2026, 1, 1), date(2026, 1, 31))[0]["day"] == "2026-01-30"
    assert reads == [(ms("2026-01-01"), ms("2026-01-25"))]  # only the days not yet read


def test_api_health_routes(tmp_path):
    days = {
        "2026-02-10": Frames(
            soh=[(ms("2026-02-10", 1), 96.5)], bms=fan_frames("2026-02-10", 2, [20])
        )
    }
    h, _ = make(tmp_path, days, "2026-02-20")
    app = create_app(
        places.store_path(tmp_path), TOKEN, UTC, health_daily=h.daily, health_fan=h.fan
    )
    c = CheckedClient(app)
    c.headers["Authorization"] = f"Bearer {TOKEN}"
    got = c.get("/health/daily", params={"from": "2026-02-01", "to": "2026-02-28"}).json()
    assert got == [
        {
            "day": "2026-02-10",
            "soh_max": 96.5,
            "isolation_min_kohm": None,
            "cell_spread_mv": None,
            "cell_spread_mv_max": None,
            "module_temp_spread_c_max": None,
            "aux_12v_rest": None,
        }
    ]
    assert c.get("/health/fan").json() == [
        {"month": "2026-02", "step": 2, "count": 1, "fan_feedback_median": 20.0}
    ]
    bad = c.get("/health/daily", params={"from": "2026-02-28", "to": "2026-02-01"})
    assert bad.status_code == 422


def test_frames_are_read_day_by_day_and_split_on_the_row_limit():
    from ev_journal.source import health_frames

    class FakeDb:
        def __init__(self):
            self.windows = []

        def query(self, q):
            if "last(" in q:
                return []
            t0, t1 = (int(x.split("ms")[0]) for x in q.split("time >= ")[1].split(" AND time < "))
            if t1 - t0 > 12 * H:  # this made-up server has a row limit
                raise RuntimeError("InfluxDB returned a partial result (row limit)")
            self.windows.append((t0, t1))
            return [{"time": t0, "soh": 97.0}] if "soh" in q else []

    db = FakeDb()
    fr = health_frames(db, ms("2026-03-01"), ms("2026-03-02", 6))
    assert [t for t, _v in fr.soh] == [ms("2026-03-01"), ms("2026-03-01", 12), ms("2026-03-02")]
    assert all(t1 - t0 <= 12 * H for t0, t1 in db.windows)
