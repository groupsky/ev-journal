import pytest

from ev_journal import proxy
from ev_journal.roadcheck import Client

KEY = "made-up-routes-key-456"
# A recorded computeRoutes answer with the field mask; made-up numbers.
ROUTES_ANSWER = {"routes": [{"distanceMeters": 12345, "duration": "987s"}]}


class Fake:
    def __init__(self, answer):
        self.answer, self.calls = answer, []

    def __call__(self, url, body=None, headers=None):
        self.calls.append((url, body, headers))
        return {}, self.answer


def test_routes_sends_a_small_mask_and_the_key_in_a_header(capsys):
    fake = Fake(ROUTES_ANSWER)
    out = proxy.routes(
        Client(fake, None, None, KEY), KEY, (0.1, 0.2), (0.3, 0.4), 1_800_000_000_000
    )
    assert out == {"duration_s": 987, "distance_m": 12345}
    ((url, body, headers),) = fake.calls
    assert url == proxy.ROUTES and KEY not in url
    assert headers == {
        "X-Goog-Api-Key": KEY,
        "X-Goog-FieldMask": "routes.duration,routes.distanceMeters",
    }
    assert body["origin"]["location"]["latLng"] == {"latitude": 0.1, "longitude": 0.2}
    assert body["destination"]["location"]["latLng"] == {"latitude": 0.3, "longitude": 0.4}
    assert body["routingPreference"] == "TRAFFIC_AWARE"
    assert body["departureTime"] == "2027-01-15T08:00:00Z"
    err = capsys.readouterr().err
    assert "call google-routes ok n=1 cost=1" in err
    assert KEY not in err and "0.1" not in err


def test_routes_without_a_route():
    out = proxy.routes(Client(Fake({}), None, None, KEY), KEY, (0.1, 0.2), (0.3, 0.4))
    assert out == {"duration_s": None, "distance_m": None}


def test_the_key_never_appears_in_routes_errors(capsys):
    def boom(url, body=None, headers=None):
        raise OSError(f"failed with {headers}")

    with pytest.raises(RuntimeError) as e:
        proxy.routes(Client(boom, None, None, KEY), KEY, (0.1, 0.2), (0.3, 0.4))
    assert KEY not in str(e.value) and "***" in str(e.value)
    assert "google-routes failed" in capsys.readouterr().err


def test_rate_limit_per_minute_and_per_day_per_token():
    lim = proxy.RateLimit(per_min=2, per_day=3)
    t = 86_400 * 20_000 + 30.0  # 30 s into a minute and a day
    assert lim.take("car", t) is None and lim.take("car", t) is None
    assert lim.take("car", t) == 31  # wait for the next minute
    assert lim.take("full", t) is None  # each token has its own count
    assert lim.take("car", t + 60) is None
    assert lim.take("car", t + 120) == 86_400 - 120 - 30 + 1  # the day is full
