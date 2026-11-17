from zoneinfo import ZoneInfo

from ev_journal import ways

UTC = ZoneInfo("UTC")
DAY = 86_400_000
DEG = 1 / 111_195  # one metre north, in degrees


def track(via_lon: float, n: int = 101, wobble: float = 0.0) -> list:
    """From a made-up place A (0.1, 0.2) north 10 km to B (0.19, 0.2),
    bending east through via_lon in the middle. wobble shifts it a little."""
    out = []
    for k in range(n):
        f = k / (n - 1)
        bend = via_lon * (1 - abs(2 * f - 1))  # 0 at the ends, via_lon in the middle
        out.append((k * 1000, 0.1 + 0.09 * f, 0.2 + bend + wobble))
    return out


def row(day: int, fixes: list, frm: str = "a", to: str = "b") -> dict:
    return {"start": day * DAY, "from": frm, "to": to, "fixes": fixes}


def test_two_competing_ways_the_most_driven_wins():
    rows = [row(d, track(0.0, wobble=d * 20 * DEG)) for d in range(4)]  # straight, 4x
    rows += [row(10 + d, track(0.02)) for d in range(2)]  # the 2 km detour, 2x
    rows += [row(20, track(0.0), "b", "a"), row(21, track(0.0), "a", "c")]  # other pairs
    w = ways.usual_way(rows, "a", "b", UTC)
    assert w["found"] and w["journeys"] == 6 and w["support"] == 4
    assert w["share"] == 0.667
    assert (w["first"], w["last"]) == ("1970-01-01", "1970-01-04")
    coords = w["line"]["coordinates"]
    assert w["line"]["type"] == "LineString" and coords[0] == [round(0.2 + 3 * 20 * DEG, 6), 0.1]
    assert all(abs(lon - 0.2) < 0.001 for lon, _lat in coords)  # the straight way
    assert len(coords) == 2  # a straight line simplifies to its ends


def test_the_detour_wins_when_driven_more():
    rows = [row(d, track(0.0)) for d in range(2)] + [row(10 + d, track(0.02)) for d in range(3)]
    w = ways.usual_way(rows, "a", "b", UTC)
    assert w["found"] and w["support"] == 3
    assert max(lon for lon, _lat in w["line"]["coordinates"]) > 0.21


def test_not_enough_data():
    few = [row(d, track(0.0)) for d in range(2)]
    w = ways.usual_way(few, "a", "b", UTC)
    assert not w["found"] and w["line"] is None and w["support"] == 2
    split = [row(d, track(0.0)) for d in range(3)]
    split += [row(10 + d, track(v)) for d, v in enumerate((0.02, -0.02, 0.04, -0.04))]
    w = ways.usual_way(split, "a", "b", UTC)  # 3 of 7 is under half
    assert not w["found"] and w["support"] == 3 and w["share"] == 0.429
    assert ways.usual_way(split, "a", "b", UTC, min_share=0.4)["found"] is True
    none = ways.usual_way([], "a", "b", UTC)
    assert not none["found"] and none["journeys"] == 0 and none["first"] is None


def test_simplify_keeps_the_bend():
    pts = track(0.02)
    s = ways.simplify(pts)
    assert len(s) == 3 and s[1] == pts[50]


def ms(y, m, d, hh, mm):
    from datetime import datetime

    return int(datetime(y, m, d, hh, mm, tzinfo=UTC).timestamp() * 1000)


RHYTHM = {"hols": set(), "ranges": (), "picks": {}}


def commute(not_me: bool = False) -> list[dict]:
    """Made-up workdays 2025-12-08 to 2026-01-09: home to work between 07:20
    and 07:40, work to home at 17:00. 2025-12-08 is a Monday."""
    from datetime import date, timedelta

    rows, d = [], date(2025, 12, 8)
    while d <= date(2026, 1, 9):
        if d.weekday() < 5:
            mm = 20 + (d.day % 5) * 5  # 20, 25, 30, 35, 40
            go = row(0, track(0.0), "home", "work")
            go["start"] = ms(d.year, d.month, d.day, 7, mm)
            back = row(0, track(0.0)[::-1], "work", "home")
            back["start"] = ms(d.year, d.month, d.day, 17, 0)
            rows += [go | {"not_me": not_me}, back | {"not_me": not_me}]
        d += timedelta(days=1)
    return rows


def test_next_trip_from_home_in_the_morning():
    t = ways.next_trip(commute(), RHYTHM, UTC, ms(2026, 1, 12, 6, 0))  # a Monday
    assert t["found"] and (t["from"], t["to"], t["day"]) == ("home", "work", "2026-01-12")
    assert t["day_type"] == "workday" and t["sure"] == 1.0 and t["pool_days"] == 20
    assert t["start"] == "2026-01-12T07:25:00+00:00" and t["end"] == "2026-01-12T07:35:00+00:00"
    assert t["way"]["found"] and t["way"]["support"] == 25


def test_next_trip_after_the_window_and_on_a_weekend():
    rows = commute()
    rows.append(row(0, track(0.0), "home", "work") | {"start": ms(2026, 1, 12, 7, 30)})
    t = ways.next_trip(rows, RHYTHM, UTC, ms(2026, 1, 12, 9, 0))  # now at work
    assert (t["from"], t["to"], t["start"][:16]) == ("work", "home", "2026-01-12T17:00")
    t = ways.next_trip(commute(), RHYTHM, UTC, ms(2026, 1, 10, 8, 0))  # a Saturday
    assert t["found"] and t["day"] == "2026-01-12" and t["from"] == "home"


def test_no_next_trip():
    now = ms(2026, 1, 12, 6, 0)
    assert not ways.next_trip(commute(), None, UTC, now)["found"]
    assert not ways.next_trip(commute(not_me=True), RHYTHM, UTC, now)["found"]
    rare = [r for r in commute() if r["start"] % (3 * DAY) < DAY]  # about a third of the days
    t = ways.next_trip(rare, RHYTHM, UTC, now)
    assert not t["found"] and t["start"] is None and t["way"] is None
