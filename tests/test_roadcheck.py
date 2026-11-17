from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from ev_journal import roadcheck as rc

UTC = ZoneInfo("UTC")
KEY_ID, SECRET = "made-up-id", "made-up-secret-123"
NOW = 1_800_000_000_000  # ms
START = NOW + 45 * 60_000
# A made-up way 5 km north from (0.1, 0.2), as GeoJSON [lon, lat].
LINE = [[0.2, 0.1], [0.2, 0.1 + 5000 / 111_195]]


def road(fid: str, level: int | None, cond: str | None = None, at_s: float = 0) -> dict:
    """A recorded Xweather route feature: level None is "no road data"."""
    props: dict = {"request": {"id": fid, "skipped": False, "success": True, "error": None}}
    if level is not None:
        period = {"timestamp": at_s, "summary": rc.LEVELS[level], "summaryIndex": level}
        if cond:
            period["roadSurface"] = {"condition": cond, "tempC": -1.0}
        props["response"] = [{"dataSource": "vaisala-road-wx", "periods": [period]}]
    else:
        props["request"] |= {"success": False, "error": {"code": "warn_no_data"}}
    return {"type": "Feature", "id": fid, "properties": props}


def meteo(air: float, dew: float, rain: float, vis: float) -> dict:
    """A recorded Open-Meteo answer: the same values every hour."""
    t0 = (NOW // 1000) - 24 * 3600
    times = [t0 + h * 3600 for h in range(72)]
    hourly = {
        "time": times,
        "temperature_2m": [air] * 72,
        "dew_point_2m": [dew] * 72,
        "precipitation": [rain] * 72,
        "visibility": [vis] * 72,
    }
    return {"latitude": 0.1, "longitude": 0.2, "hourly": hourly}


class Fake:
    """Answers in order; records the calls."""

    def __init__(self, answers):
        self.answers, self.calls = list(answers), []

    def __call__(self, url, body=None):
        self.calls.append((url, body))
        return self.answers.pop(0)


def client(fake):
    return rc.Client(fake, KEY_ID, SECRET)


def test_points_every_km_with_the_end():
    pts = rc.points_along(LINE)
    assert [round(p[0], 3) for p in pts] == [0, 1, 2, 3, 4, 5]
    assert pts[0][1:] == (0.1, 0.2)
    assert rc.points_along([]) == []


def test_due_only_for_a_likely_trip_with_a_way():
    trip = {"found": True, "from": "a", "to": "b", "start": "2026-01-05T08:00:00+00:00"}
    trip["way"] = {"line": {"coordinates": LINE}}
    at = rc.due_at(trip, 45)
    assert at == datetime(2026, 1, 5, 7, 15, tzinfo=UTC).timestamp() * 1000
    assert rc.due_at(trip | {"way": None}, 45) is None
    assert rc.due_at({"found": False, "start": None, "way": None}, 45) is None


def test_summary_first_detail_only_where_not_green():
    fake = Fake(
        [
            (
                {"X-RateLimit-Remaining-Period": "9000"},
                {"success": True, "response": [road(str(i), 1 if i == 3 else 0) for i in range(6)]},
            ),
            (
                {"X-RateLimit-Remaining-Period": "8990"},
                {"success": True, "response": [road("0", 2, "ICE")]},
            ),
        ]
    )
    out = rc.check(LINE, START, NOW, client(fake), UTC)
    assert len(fake.calls) == 2
    (u1, b1), (u2, b2) = fake.calls
    assert "/roadweather/route?" in u1 and "mindist=" in u1 and len(b1) == 6
    assert "/roadweather/conditions/route?" in u2 and "mindist=" in u2 and len(b2) == 1
    assert b2[0]["p"] == b1[3]["p"] and b1[0]["from"] == "+45minutes"
    assert out["level"] == "YELLOW"  # the level of the summary
    assert out["items"] == [
        {
            "km": 3.0,
            "level": "YELLOW",
            "what": ["ice"],
            "at": out["items"][0]["at"],
            "source": "road",
        }
    ]
    assert out["road_points"] == 6 and out["estimate_points"] == 0
    assert out["quota_left"] == 8990 and out["skipped"] is None


def test_all_green_asks_no_detail():
    fake = Fake([({}, {"success": True, "response": [road(str(i), 0) for i in range(6)]})])
    out = rc.check(LINE, START, NOW, client(fake), UTC)
    assert len(fake.calls) == 1 and out["level"] == "GREEN" and out["items"] == []


def test_estimate_where_no_road_data():
    fake = Fake(
        [
            (
                {},
                {
                    "success": True,
                    "response": [road(str(i), None if i < 2 else 0) for i in range(6)],
                },
            ),
            ({}, [meteo(-1.0, -3.0, 0.4, 20000), meteo(5.0, 4.0, 0.0, 150)]),
        ]
    )
    out = rc.check(LINE, START, NOW, client(fake), UTC)
    url, body = fake.calls[1]
    assert url.startswith(rc.OPEN_METEO) and body is None and SECRET not in url
    assert out["level"] == "RED" and out["estimate_points"] == 2 and out["road_points"] == 4
    assert [(i["km"], i["level"], i["what"], i["source"]) for i in out["items"]] == [
        (0.0, "RED", ["ice"], "estimate"),
        (1.0, "RED", ["fog"], "estimate"),
    ]


@pytest.mark.parametrize(
    ("air", "dew", "rain", "vis", "level", "what"),
    [
        (2.0, -5.0, 0.1, 9000, "YELLOW", ["ice"]),  # cold and wet
        (2.0, 1.0, 0.0, 9000, "YELLOW", ["ice"]),  # near the dew point
        (2.1, 1.5, 0.1, 9000, "GREEN", []),
        (0.0, -5.0, 0.1, 9000, "RED", ["ice"]),
        (0.0, -5.0, 0.0, 9000, "GREEN", []),  # dry and far from dew point
        (8.0, 2.0, 0.0, 999, "YELLOW", ["fog"]),
        (8.0, 2.0, 0.0, 199, "RED", ["fog"]),
    ],
)
def test_estimate_rules(air, dew, rain, vis, level, what):
    hourly = meteo(air, dew, rain, vis)["hourly"]
    assert rc.estimate(hourly, NOW / 1000, rc.Rules()) == (level, what)


def test_rain_counts_only_in_the_last_hours():
    hourly = meteo(0.0, -5.0, 0.0, 9000)["hourly"]
    i = hourly["time"].index(NOW // 1000 - (NOW // 1000 - hourly["time"][0]) % 3600)
    hourly["precipitation"][i - 6] = 1.0  # 7 hours before
    assert rc.estimate(hourly, NOW / 1000, rc.Rules())[0] == "GREEN"
    hourly["precipitation"][i - 5] = 1.0  # 6 hours before
    assert rc.estimate(hourly, NOW / 1000, rc.Rules())[0] == "RED"


def test_skips_below_the_quota_reserve():
    fake = Fake([])
    out = rc.check(LINE, START, NOW, client(fake), UTC, reserve=1000, quota_left=1003)
    assert fake.calls == [] and out["skipped"] == "quota" and out["level"] is None
    # The header of the summary answer says the quota is low: no detail call.
    fake = Fake(
        [
            (
                {"x-ratelimit-remaining-period": "1004"},
                {"success": True, "response": [road(str(i), 2 if i == 3 else 0) for i in range(6)]},
            )
        ]
    )
    out = rc.check(LINE, START, NOW, client(fake), UTC, reserve=1000)
    assert len(fake.calls) == 1 and out["quota_left"] == 1004
    assert out["skipped"] == "detail: quota" and out["level"] == "RED"


def test_the_key_never_appears_in_errors_or_output():
    def boom(url, body=None):
        raise OSError(f"failed: {url}")

    with pytest.raises(RuntimeError) as e:
        rc.check(LINE, START, NOW, client(boom), UTC)
    assert SECRET not in str(e.value) and KEY_ID not in str(e.value) and "***" in str(e.value)
    fake = Fake([({}, {"success": True, "response": [road(str(i), 0) for i in range(6)]})])
    out = rc.check(LINE, START, NOW, client(fake), UTC)
    assert SECRET not in repr(out) and KEY_ID not in repr(out)
