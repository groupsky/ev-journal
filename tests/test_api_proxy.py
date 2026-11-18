"""The proxy for the car and the car token (#70). Made-up points near 0 N 0 E."""

from zoneinfo import ZoneInfo

import pytest
from conftest import SPEC, TOKEN, CheckedClient

from ev_journal import __main__ as main
from ev_journal import places, proxy
from ev_journal.api import CAR_ROUTES, create_app
from ev_journal.settings import Settings

UTC = ZoneInfo("UTC")
CAR = "test-car-token"
ID, SECRET, GKEY = "made-up-id", "made-up-secret-123", "made-up-routes-key-456"
ROAD = {"points": [{"lat": 0.1, "lon": 0.2, "at": "2027-01-15T08:00:00Z"}]}
ROUTES = {"origin": {"lat": 0.1, "lon": 0.2}, "destination": {"lat": 0.3, "lon": 0.4}}


def settings(tmp_path) -> Settings:
    files = {"ID": ID, "SECRET": SECRET, "KEY": GKEY}
    for k, v in files.items():
        (tmp_path / k).write_text(v + "\n")
    return Settings.from_env(
        {
            "EVJ_XWEATHER_ID_FILE": str(tmp_path / "ID"),
            "EVJ_XWEATHER_SECRET_FILE": str(tmp_path / "SECRET"),
            "EVJ_GOOGLE_ROUTES_KEY_FILE": str(tmp_path / "KEY"),
        }
    )


class Fake:
    """Recorded outside answers, in order."""

    def __init__(self, answers):
        self.answers, self.calls = list(answers), []

    def __call__(self, url, body=None, headers=None):
        self.calls.append((url, body, headers))
        a = self.answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return a


def app_client(data_dir, s, fake, limit=None, who=TOKEN):
    state = {"road_quota": None}
    app = create_app(
        places.store_path(data_dir),
        TOKEN,
        UTC,
        car_token=CAR,
        road_proxy=lambda pts: main.proxy_road(s, state, pts, fake),
        routes_proxy=lambda o, d, t: main.proxy_routes(s, o, d, t, fake),
        limit=limit,
    )
    c = CheckedClient(app)
    c.headers["Authorization"] = f"Bearer {who}"
    return c, state


def green(n):
    feats = [
        {
            "id": str(i),
            "properties": {
                "request": {"id": str(i), "success": True},
                "response": [{"periods": [{"timestamp": 0, "summaryIndex": 0}]}],
            },
        }
        for i in range(n)
    ]
    return {"X-RateLimit-Remaining-Period": "7000"}, {"success": True, "response": feats}


def test_keys_come_from_files_and_are_not_shown(tmp_path):
    s = settings(tmp_path)
    assert (s.xweather_id, s.xweather_secret, s.routes_key) == (ID, SECRET, GKEY)
    assert not any(k in repr(s) for k in (ID, SECRET, GKEY))


def test_road_proxy_shares_the_quota(tmp_path, data_dir):
    fake = Fake([green(1)])
    c, state = app_client(data_dir, settings(tmp_path), fake, who=CAR)
    r = c.post("/proxy/road", json=ROAD)
    assert r.status_code == 200, r.text
    assert r.json()["points"] == [
        {"level": "GREEN", "what": [], "at": "2027-01-15T08:00+00:00", "source": "road"}
    ]
    assert r.json()["quota_left"] == 7000 and state["road_quota"][0] == 7000
    assert fake.calls[0][1][0]["p"] == "0.100000,0.200000"


def test_routes_proxy(tmp_path, data_dir):
    fake = Fake([({}, {"routes": [{"distanceMeters": 12345, "duration": "987s"}]})])
    c, _ = app_client(data_dir, settings(tmp_path), fake, who=CAR)
    r = c.post("/proxy/routes", json=ROUTES | {"departure": "2000-01-01T00:00:00Z"})
    assert r.json() == {"duration_s": 987, "distance_m": 12345}
    assert "departureTime" not in fake.calls[0][1]  # a past departure is now


def test_keys_never_in_errors_answers_or_logs(tmp_path, data_dir, capsys):
    boom = OSError(f"failed: {ID} {SECRET} {GKEY}")
    c, _ = app_client(data_dir, settings(tmp_path), Fake([boom, boom]))
    for path, body in (("/proxy/road", ROAD), ("/proxy/routes", ROUTES)):
        r = c.post(path, json=body)
        assert r.status_code == 502 and r.json() == {"detail": "the outside call failed"}
    out = capsys.readouterr()
    for k in (ID, SECRET, GKEY):
        assert k not in out.err and k not in out.out
    assert "***" in out.err and "0.1" not in out.err


def test_no_key_is_503(data_dir):
    app = create_app(places.store_path(data_dir), TOKEN, UTC)
    c = CheckedClient(app)
    c.headers["Authorization"] = f"Bearer {TOKEN}"
    assert c.post("/proxy/road", json=ROAD).status_code == 503
    assert c.post("/proxy/routes", json=ROUTES).status_code == 503


def test_rate_limit_gives_429_with_retry_after(tmp_path, data_dir):
    fake = Fake([green(1)])
    lim = proxy.RateLimit(per_min=1, per_day=100)
    c, _ = app_client(data_dir, settings(tmp_path), fake, lim, who=CAR)
    assert c.post("/proxy/road", json=ROAD).status_code == 200
    r = c.post("/proxy/road", json=ROAD)
    assert r.status_code == 429 and 1 <= int(r.headers["Retry-After"]) <= 61
    assert len(fake.calls) == 1
    # The full token has its own count.
    c.headers["Authorization"] = f"Bearer {TOKEN}"
    fake.answers.append(green(1))
    assert c.post("/proxy/road", json=ROAD).status_code == 200


def test_car_token_may_call_only_its_routes(tmp_path, data_dir):
    c, _ = app_client(data_dir, settings(tmp_path), Fake([]), who=CAR)
    assert c.get("/places").status_code == 200
    assert c.get("/journeys").status_code == 200
    assert c.get("/journeys/days").status_code == 200
    assert c.get("/journeys/1/track").status_code == 404  # allowed, just unknown
    assert c.get("/upkeep").status_code == 200
    assert c.get("/tyres").status_code == 200
    assert c.get("/health/fan").status_code == 200
    assert c.get("/advice").status_code == 404  # allowed, no advice yet
    # Everything else: 403, also before the body is checked.
    assert c.post("/places", json={}).status_code == 403
    assert c.get("/routes").status_code == 403
    assert c.get("/places/suggestions").status_code == 403
    assert c.get("/road-check").status_code == 403
    assert c.post("/commands", json={}).status_code == 403
    assert c.get("/upkeep/x/log").status_code == 403


@pytest.mark.parametrize("method,path", sorted(CAR_ROUTES))
def test_car_routes_exist(method, path):
    assert method.lower() in SPEC["paths"][path]
