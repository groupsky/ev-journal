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


DAY = 86_400_000
T0 = 1_768_190_400_000  # 2026-01-12T04:00:00Z, a made-up Monday


def loop_state() -> dict:
    """Made-up journeys from a to b at 08:00 UTC and back at 18:00, 14 days."""
    fixes = [(k * 1000, 0.1 + k * 50 / 111_195, 0.2) for k in range(101)]  # 5 km
    rows = []
    for d in range(1, 15):
        for frm, to, h in (("a", "b", 4), ("b", "a", 14)):
            go = {"start": T0 - d * DAY + h * 3_600_000, "fixes": fixes, "not_me": False}
            rows.append(go | {"from": frm, "to": to})
    rhythm = {"hols": set(), "ranges": (), "picks": {"workday": (), "off": ()}}
    state = {"way_rows": rows, "rhythm": rhythm}
    return state | {"road_trip": None, "road_done": None, "road_quota": None, "road_check": None}


def keyed(tmp_path, **env):
    from ev_journal.settings import Settings

    (tmp_path / "id").write_text(KEY_ID)
    (tmp_path / "secret").write_text(SECRET)
    files = {"EVJ_XWEATHER_ID_FILE": str(tmp_path / "id")}
    files["EVJ_XWEATHER_SECRET_FILE"] = str(tmp_path / "secret")
    return Settings.from_env(files | {"EVJ_MQTT_URL": "mqtt://broker"} | env)


def test_the_check_runs_the_lead_time_before_a_likely_trip(tmp_path, monkeypatch, capsys):
    from ev_journal import __main__ as cli
    from ev_journal import mqtt

    sent = []
    monkeypatch.setattr(
        mqtt, "publish", lambda url, m, topic="", retain=True: sent.append((topic, m))
    )
    s, state = keyed(tmp_path), loop_state()
    due = T0 + 4 * 3_600_000 - 45 * 60_000  # 07:15
    assert cli.road_due(s, state, T0) == due  # wait until then
    fake = Fake([({"X-RateLimit-Remaining-Period": "5000"}, {"success": True, "response": []})])
    fake.answers.append(({}, [meteo(8.0, 2.0, 0.0, 9000)] * 6))
    out = cli.road_check(s, state, due, fake)
    assert out["trip"]["from"] == "a" and out["for"] == "2026-01-12T08:00+00:00"
    assert out["level"] == "GREEN" and state["road_quota"] == (5000, due)
    assert sent == [(mqtt.ROAD_TOPIC, out)] and state["road_check"] is out
    assert cli.road_due(s, state, due + 60_000) is None  # once per trip
    assert (
        SECRET not in repr(sent) and SECRET not in repr(s) and SECRET not in capsys.readouterr().err
    )


def test_no_check_without_a_likely_trip_or_a_key(tmp_path):
    from ev_journal import __main__ as cli
    from ev_journal.settings import Settings

    state = loop_state()
    assert cli.road_due(Settings.from_env({}), state, T0) is None  # no key
    s = keyed(tmp_path)
    assert cli.road_due(s, state | {"rhythm": None}, T0) is None
    assert cli.road_due(s, state | {"way_rows": state["way_rows"][:2]}, T0) is None


def test_road_check_api(data_dir):
    from conftest import TOKEN, CheckedClient

    from ev_journal import places
    from ev_journal.api import create_app

    last = {"value": None}
    app = create_app(places.store_path(data_dir), TOKEN, UTC, road_check=lambda: last["value"])
    c = CheckedClient(app)
    c.headers["Authorization"] = f"Bearer {TOKEN}"
    assert c.get("/road-check").status_code == 404
    fake = Fake(
        [({}, {"success": True, "response": [road(str(i), 2 if i == 1 else 0) for i in range(6)]})]
    )
    fake.answers.append(({}, {"success": True, "response": [road("0", 2, "ICE")]}))
    trip = {"from": "a", "to": "b", "start": "x", "end": "y"}
    last["value"] = {"trip": trip} | rc.check(LINE, START, NOW, client(fake), UTC)
    got = c.get("/road-check").json()
    assert got["level"] == "RED" and got["for"] == last["value"]["for"]
    assert got["items"][0] | {"at": None} == {
        "km": 1.0,
        "level": "RED",
        "what": ["ice"],
        "at": None,
        "source": "road",
    }


def test_a_low_quota_from_last_month_does_not_block():
    from ev_journal import __main__ as cli

    assert cli.quota_now((900, T0), T0 + DAY) == 900
    assert cli.quota_now((900, T0 - 20 * DAY), T0) is None  # read in December
    assert cli.quota_now(None, T0) is None


def test_a_failed_estimate_keeps_the_road_data(capsys):
    def flaky(url, body=None):
        if url.startswith(rc.OPEN_METEO):
            raise OSError("429")
        feats = [road(str(i), None if i == 0 else 2 if i == 1 else 0) for i in range(6)]
        return {}, {"success": True, "response": feats}

    out = rc.check(LINE, START, NOW, client(flaky), UTC, quota_left=None, reserve=0)
    assert out["level"] == "RED" and out["skipped"] == "estimate"
    assert out["road_points"] == 5 and out["estimate_points"] == 0
    assert "estimate failed" in capsys.readouterr().err


def test_points_match_by_request_id_or_order():
    feats = [road(str(i), 2 if i == 2 else 0) for i in range(6)]
    for f in feats:
        del f["id"]
    by_req = rc._by_id({"success": True, "response": feats})
    assert list(by_req) == [str(i) for i in range(6)]
    for f in feats:
        del f["properties"]["request"]["id"]
    assert list(rc._by_id({"success": False, "response": feats})) == [str(i) for i in range(6)]


def test_nothing_known_is_not_green():
    def flaky(url, body=None):
        if url.startswith(rc.OPEN_METEO):
            raise OSError("429")
        return {}, {"success": False, "response": [road(str(i), None) for i in range(6)]}

    out = rc.check(LINE, START, NOW, client(flaky), UTC)
    assert out["level"] is None and out["skipped"] == "estimate" and out["items"] == []


def test_points_from_the_car_get_one_answer_each(capsys):
    """The proxy (#70): summary first, detail where not GREEN, the estimate
    where there is no road data; answers in the order sent."""
    pts = [(0.1, 0.2, START), (0.11, 0.2, START + 60_000), (0.12, 0.2, START + 120_000)]
    fake = Fake(
        [
            (
                {"X-RateLimit-Remaining-Period": "9000"},
                {"success": True, "response": [road("0", 0), road("1", 2), road("2", None)]},
            ),
            ({"X-RateLimit-Remaining-Period": "8995"}, {"response": [road("0", 2, "ICE")]}),
            ({}, meteo(-3, -4, 1.0, 5000)),
        ]
    )
    out = rc.at_points(pts, NOW, client(fake), UTC)
    (u1, b1), (u2, b2), (u3, _b3) = fake.calls
    assert "mindist=" in u1 and len(b1) == 3 and len(b2) == 1 and b2[0]["p"] == b1[1]["p"]
    assert "open-meteo" in u3
    assert [p["level"] for p in out["points"]] == ["GREEN", "RED", "RED"]
    assert [p["what"] for p in out["points"]] == [[], ["ice"], ["ice"]]
    assert [p["source"] for p in out["points"]] == ["road", "road", "estimate"]
    assert out["points"][0]["at"] == rc._iso(START, UTC)
    assert out["quota_left"] == 8995 and out["skipped"] is None
    # One log line per outside call: API, size, cost, quota, time; no coordinates.
    log = [ln for ln in capsys.readouterr().err.splitlines() if " call " in ln]
    assert len(log) == 3
    assert "xweather-summary ok n=3 cost=3 quota_left=9000 ms=" in log[0]
    assert "xweather-conditions ok n=1 cost=5 quota_left=8995 ms=" in log[1]
    assert "open-meteo ok n=1 cost=0" in log[2]
    assert not any(x in "\n".join(log) for x in ("0.1", "0.2", KEY_ID, SECRET))


def test_points_below_the_reserve_make_no_call():
    pts = [(0.1, 0.2, START)]
    fake = Fake([])
    out = rc.at_points(pts, NOW, client(fake), UTC, reserve=1000, quota_left=1000)
    assert fake.calls == [] and out["skipped"] == "quota"
    assert out["points"] == [
        {"level": None, "what": [], "at": rc._iso(START, UTC), "source": "none"}
    ]
