import json
import sqlite3
from zoneinfo import ZoneInfo

from conftest import SPEC, TOKEN, CheckedClient
from week import PLACES, ms, week

from ev_journal import places
from ev_journal.api import create_app, openapi
from ev_journal.journeys import build

UTC = ZoneInfo("UTC")
LAKE = {"name": "Test Lake", "lat": 0.7, "lon": -0.2, "kind": "other", "aliases": ["lake"]}


def test_openapi_file_is_up_to_date():
    # Regenerate with: python -m ev_journal.api > docs/openapi.json
    assert SPEC == json.loads(json.dumps(openapi()))


def test_without_token_is_refused(client, data_dir):
    for headers in ({"Authorization": ""}, {"Authorization": "Bearer wrong"}):
        assert client.get("/places", headers=headers).status_code == 401
        assert client.post("/commands", headers=headers, json={}).status_code == 401
    # A server with no token configured refuses everything.
    bare = CheckedClient(create_app(places.store_path(data_dir), None, UTC))
    assert bare.get("/places", headers={"Authorization": "Bearer "}).status_code == 401


def test_place_create_change_delete(client):
    w = client.post("/places", json=LAKE).json()
    pid, v1 = w["id"], w["version"]
    assert v1 == 1 and not w["stale"]
    got = client.get(f"/places/{pid}").json()
    assert got["name"] == "Test Lake" and got["aliases"] == ["lake"]
    w = client.put(f"/places/{pid}", json={**LAKE, "name": "Test Pond", "version": v1}).json()
    assert w["version"] == 2 and not w["stale"]
    assert client.get(f"/places/{pid}").json()["name"] == "Test Pond"
    w = client.delete(f"/places/{pid}", params={"version": 2}).json()
    assert w["version"] == 3
    assert client.get(f"/places/{pid}").status_code == 404
    assert client.put(f"/places/{pid}", json=LAKE).status_code == 404
    assert client.get("/places").json()["items"] == []


def test_stale_version_last_write_wins(client):
    pid = client.post("/places", json=LAKE).json()["id"]
    client.put(f"/places/{pid}", json={**LAKE, "name": "Phone A", "version": 1})
    w = client.put(f"/places/{pid}", json={**LAKE, "name": "Phone B", "version": 1}).json()
    assert w["stale"] and w["version"] == 3
    assert client.get(f"/places/{pid}").json()["name"] == "Phone B"


def test_changed_since_with_deleted_rows(client):
    a = client.post("/places", json=LAKE).json()["id"]
    b = client.post("/places", json={**LAKE, "name": "Test B"}).json()["id"]
    since = client.get("/places").json()["cursor"]
    client.put(f"/places/{a}", json={**LAKE, "name": "Test A2"})
    client.delete(f"/places/{b}")
    got = client.get("/places", params={"changed_since": since}).json()
    assert [p["id"] for p in got["items"]] == [a]
    assert [d["id"] for d in got["deleted"]] == [b]
    later = client.get("/places", params={"changed_since": got["cursor"]}).json()
    assert later["items"] == [] and later["deleted"] == []


def test_routes(client):
    a = client.post("/places", json=LAKE).json()["id"]
    b = client.post("/places", json={**LAKE, "name": "Test B", "lat": 0.5}).json()["id"]
    assert client.post("/routes", json={"name": "r", "place_ids": [a, "nope"]}).status_code == 422
    w = client.post("/routes", json={"name": "Test Route", "place_ids": [a, b]}).json()
    rid = w["id"]
    since = client.get("/routes").json()["cursor"]
    w = client.put(f"/routes/{rid}", json={"name": "Back", "place_ids": [b, a], "version": 1})
    assert w.json()["version"] == 2
    assert client.get(f"/routes/{rid}").json()["place_ids"] == [b, a]
    assert [
        r["id"] for r in client.get("/routes", params={"changed_since": since}).json()["items"]
    ] == [rid]
    client.delete(f"/routes/{rid}")
    assert client.get(f"/routes/{rid}").status_code == 404
    got = client.get("/routes", params={"changed_since": since}).json()
    assert got["items"] == [] and [d["id"] for d in got["deleted"]] == [rid]


def test_label_and_not_me_survive_a_recompute(client, data_dir, journeys):
    # Tue's journey starts at 09:00. A label set at 09:30 attaches to it.
    w = week()
    journeys += [(j.start, j.end) for j in build(w.trips, w.charges, w.fixes, PLACES, UTC).journeys]
    tue = ms(6, 9)
    assert client.put(f"/journeys/{ms(6, 3)}/label", json={"label": "other"}).status_code == 404
    r = client.put(
        f"/journeys/{tue + 30 * 60_000}/label", json={"label": "commute", "not_me": True}
    )
    assert r.status_code == 200
    db = places.open_store(data_dir)
    for _ in range(2):  # every run recomputes from scratch
        w = week()
        res = build(w.trips, w.charges, w.fixes, PLACES, UTC, labels=places.all_labels(db))
        j = next(j for j in res.journeys if j.start == tue)
        assert j.label == "commute" and j.label_by_hand and j.not_me
        others = [x for x in res.journeys if x.start != tue]
        assert not any(x.not_me for x in others)
    # Clearing the label by hand gives back the computed one.
    client.put(f"/journeys/{tue}/label", json={"label": None, "not_me": False})
    res = build(w.trips, w.charges, w.fixes, PLACES, UTC, labels=places.all_labels(db))
    j = next(j for j in res.journeys if j.start == tue)
    assert j.label == "other" and not j.not_me


def test_suggestion_turned_into_a_place(client, stops):
    # 3 visits near a spot with no place, and 1 visit somewhere else.
    stops += [(ms(5, 18), 0.4, -0.4), (ms(6, 18), 0.4001, -0.4), (ms(7, 19), 0.4, -0.4001)]
    stops += [(ms(8, 12), -0.6, 0.6)]
    got = client.get("/places/suggestions").json()
    assert len(got) == 1
    s = got[0]
    assert s["visits"] == 3 and s["typical_hour"] == 18
    client.post("/places", json={"name": "Test Gym", "lat": s["lat"], "lon": s["lon"]})
    assert client.get("/places/suggestions").json() == []


def test_repeated_command_post_is_ignored(client, data_dir):
    cmd = {
        "client_id": "c-1",
        "time": ms(5, 8),
        "text": "navigate to test lake",
        "action": "navigate",
    }
    first = client.post("/commands", json=cmd)
    again = client.post("/commands", json={**cmd, "text": "changed"})
    assert first.status_code == 201 and again.status_code == 200
    assert again.json()["text"] == "navigate to test lake"
    db = sqlite3.connect(places.store_path(data_dir))
    assert db.execute("SELECT count(*) FROM commands").fetchone()[0] == 1


def test_seed_is_a_first_import_only(tmp_path):
    seed = tmp_path / "seed.json"
    seed.write_text(
        json.dumps([{"id": "p1", "name": "Test Home", "kind": "home", "lat": 0.1, "lon": 0.1}])
    )
    db = places.open_store(tmp_path)
    assert places.load_seed(db, seed)
    seed.write_text("[]")
    assert not places.load_seed(db, seed)
    assert [p.id for p in places.all_places(db)] == ["p1"]


def test_store_from_issue_3_is_upgraded(tmp_path):
    old = sqlite3.connect(places.store_path(tmp_path))
    old.execute(
        "CREATE TABLE places (id TEXT PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL,"
        " lat REAL NOT NULL, lon REAL NOT NULL, radius_m REAL NOT NULL)"
    )
    old.execute("INSERT INTO places VALUES ('p1', 'Test Home', 'home', 0.1, 0.1, 150)")
    old.commit()
    old.close()
    db = places.open_store(tmp_path)
    assert [p.id for p in places.all_places(db)] == ["p1"]
    row = db.execute("SELECT version, deleted, aliases, seq FROM places").fetchone()
    assert tuple(row) == (1, 0, "[]", 1)


def test_errors_have_one_shape(client):
    a = client.post("/places", json=LAKE).json()["id"]
    rid = client.post("/routes", json={"name": "r", "place_ids": [a]}).json()["id"]
    for resp in (
        client.post("/routes", json={"name": "", "place_ids": [a]}),
        client.put(f"/routes/{rid}", json={"name": "r", "place_ids": ["nope"]}),
        client.post("/places", json={"name": "x"}),
        client.get("/places", params={"changed_since": "soon"}),
    ):
        assert resp.status_code == 422 and isinstance(resp.json()["detail"], str)
    unknown = client.put("/routes/nope", json={"name": "r", "place_ids": ["nope"]})
    assert unknown.status_code == 404


def test_a_place_in_a_route_cannot_be_deleted(client):
    a = client.post("/places", json=LAKE).json()["id"]
    rid = client.post("/routes", json={"name": "r", "place_ids": [a]}).json()["id"]
    assert client.delete(f"/places/{a}").status_code == 409
    client.delete(f"/routes/{rid}")
    assert client.delete(f"/places/{a}").status_code == 200


def test_bearer_scheme_ignores_case(client):
    assert client.get("/places", headers={"Authorization": f"bearer {TOKEN}"}).status_code == 200


def test_journey_track(data_dir):
    pts = [{"t": 1, "lat": 0.1, "lon": 0.2, "kwh_per_100km": None}]
    pts.append({"t": 9, "lat": 0.1005, "lon": 0.2, "kwh_per_100km": 14.5})
    tracks = {7: pts, 8: []}
    c = CheckedClient(create_app(places.store_path(data_dir), TOKEN, UTC, track=tracks.get))
    c.headers["Authorization"] = f"Bearer {TOKEN}"
    assert c.get("/journeys/7/track").json() == pts
    assert c.get("/journeys/8/track").json() == []
    r = c.get("/journeys/9/track")
    assert r.status_code == 404 and r.json() == {"detail": "no such journey"}
