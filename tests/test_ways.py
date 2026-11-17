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
