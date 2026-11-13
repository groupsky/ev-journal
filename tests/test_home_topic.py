"""The retained home place on homy/ioniq/home (#50). Made-up places near 0 N 0 E."""

from types import SimpleNamespace
from zoneinfo import ZoneInfo

from conftest import TOKEN, CheckedClient
from week import HOME, LAKE, WORK

from ev_journal import __main__ as cli
from ev_journal import mqtt, outputs, places
from ev_journal.api import create_app

NOW = 1_768_039_200_000  # 2026-01-10T10:00:00Z
HOME_IN = {"name": "Test Home", "kind": "home", "lat": 0.1, "lon": 0.1, "radius_m": 150}


def test_message_with_the_home_place():
    assert outputs.home_message([WORK, HOME, LAKE], NOW) == {
        "updated": "2026-01-10T10:00:00Z",
        "id": "p-home",
        "lat": 0.1,
        "lon": 0.1,
        "radius_m": 150,
    }


def test_no_message_without_a_home_place():
    assert outputs.home_message([WORK, LAKE], NOW) is None


def test_several_homes_the_first_by_id_with_a_warning(capsys):
    other = places.Place("a-home", "Test Home 2", "home", 0.2, 0.2, 80)
    assert outputs.home_message([HOME, other], NOW)["id"] == "a-home"
    assert "2 home places, using a-home" in capsys.readouterr().err


def fake_mqtt(monkeypatch, tmp_path):
    sent = []
    monkeypatch.setattr(
        mqtt, "publish", lambda url, m, topic="", retain=True: sent.append((topic, retain, m))
    )
    return SimpleNamespace(mqtt_url="mqtt://broker", data_dir=tmp_path), sent


def test_published_retained_from_the_store(monkeypatch, data_dir):
    s, sent = fake_mqtt(monkeypatch, data_dir)
    cli.publish_home(s)
    assert sent == []  # no home place yet
    client = CheckedClient(
        create_app(places.store_path(data_dir), TOKEN, ZoneInfo("UTC")),
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    pid = client.post("/places", json=HOME_IN).json()["id"]
    cli.publish_home(s)
    [(topic, retain, m)] = sent
    assert topic == "homy/ioniq/home" and retain is True
    assert (m["id"], m["lat"], m["lon"], m["radius_m"]) == (pid, 0.1, 0.1, 150)


def test_a_change_through_the_api_publishes_again(monkeypatch, data_dir):
    s, sent = fake_mqtt(monkeypatch, data_dir)
    client = CheckedClient(
        create_app(
            places.store_path(data_dir),
            TOKEN,
            ZoneInfo("UTC"),
            places_changed=lambda: cli.publish_home(s),
        ),
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    pid = client.post("/places", json=HOME_IN).json()["id"]
    assert client.put(f"/places/{pid}", json={**HOME_IN, "radius_m": 300}).status_code == 200
    assert [m["radius_m"] for _, _, m in sent] == [150, 300]


def test_a_failed_publish_does_not_fail_the_write(data_dir, capsys):
    def broken():
        raise OSError("broker down")

    client = CheckedClient(
        create_app(places.store_path(data_dir), TOKEN, ZoneInfo("UTC"), places_changed=broken),
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    assert client.post("/places", json=HOME_IN).status_code == 201
    assert "broker down" in capsys.readouterr().err


def test_soon_publishes_in_the_background_and_logs_a_failure(monkeypatch, data_dir, capsys):
    import threading

    s, _ = fake_mqtt(monkeypatch, data_dir)
    done = threading.Event()

    def broken(_s):
        done.set()
        raise OSError("broker down")

    monkeypatch.setattr(cli, "publish_home", broken)
    cli.publish_home_soon(s)
    assert done.wait(5)
    for t in threading.enumerate():
        if t is not threading.current_thread() and t.daemon:
            t.join(1)
    assert "broker down" in capsys.readouterr().err
