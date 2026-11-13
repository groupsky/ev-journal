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


def item(i, t=None, **kw):
    """A made-up journey item; t is its start in ms."""
    return {
        "id": t or 1_000_000 * (i + 1), "start": f"s{i}", "end": f"e{i}", "from": "Test Bay",
        "to": None, "from_place_id": "1", "to_place_id": None, "label": None, "not_me": False,
        "km": 1.0, "kwh_used": 0.2, "drive_min": 3, "waiting_min": 0, "charge_min": 0,
        "charges": 0, "kwh_charged": 0.0, "cost_eur": None, **kw,
    }  # fmt: skip


def journeys_client(data_dir, items, tz=UTC):
    c = CheckedClient(
        create_app(places.store_path(data_dir), TOKEN, tz, journey_list=lambda: items)
    )
    c.headers["Authorization"] = f"Bearer {TOKEN}"
    return c


def test_journeys_pages_join_without_gap_or_duplicate(data_dir):
    items = [item(i) for i in range(25)]  # oldest first
    c = journeys_client(data_dir, items)
    first = c.get("/journeys", params={"limit": 10}).json()
    assert first[0] == items[-1] and first[0]["from"] == "Test Bay"
    second = c.get("/journeys", params={"limit": 10, "before": first[-1]["id"]}).json()
    ids = [j["id"] for j in first + second]
    assert ids == [j["id"] for j in items[::-1][:20]]
    assert len(c.get("/journeys").json()) == 20  # the default limit
    assert c.get("/journeys", params={"before": items[0]["id"]}).json() == []
    assert c.get("/journeys", params={"limit": 101}).status_code == 422


T0, H = 1_000_000_000, 3_600_000


def made_up_mix():
    # 30 journeys, one every 7 h: some with a charge, some long, two places
    # with the same name told apart by their ids.
    return [
        item(
            i,
            t=1_000_000_000 + i * 7 * 3_600_000,
            km=float(i % 7) * 10,
            kwh_used=float(i % 7) * 1.5,
            charges=1 if i % 3 == 0 else 0,
            kwh_charged=5.0 if i % 3 == 0 else 0.0,
            cost_eur=1.25 if i % 6 == 0 else None,
            **{"from": "Test Bay", "to": "Test Bay"},
            from_place_id="a" if i % 2 else "b",
            to_place_id="c" if i % 5 == 0 else None,
        )
        for i in range(30)
    ]


FILTERS = [
    ({"from": 1_000_000_000 + 50 * 3_600_000}, lambda j: j["id"] >= 1_000_000_000 + 50 * 3_600_000),
    ({"to": 1_000_000_000 + 70 * 3_600_000}, lambda j: j["id"] <= 1_000_000_000 + 70 * 3_600_000),
    ({"with_charge": "true"}, lambda j: j["charges"] > 0),
    ({"with_charge": "false"}, lambda j: j["charges"] == 0),
    (
        {"from": T0 + 50 * H, "to": T0 + 150 * H, "min_km": 20},
        lambda j: T0 + 50 * H <= j["id"] <= T0 + 150 * H and j["km"] >= 20,
    ),
    ({"min_km": 30}, lambda j: j["km"] >= 30),
    ({"place": "a"}, lambda j: "a" in (j["from_place_id"], j["to_place_id"])),
    ({"place": "c"}, lambda j: "c" in (j["from_place_id"], j["to_place_id"])),
]


def page_all(c, params, limit=4):
    got, before = [], None
    while True:
        p = {**params, "limit": limit, **({"before": before} if before else {})}
        page = c.get("/journeys", params=p).json()
        if not page:
            return got
        got += page
        before = page[-1]["id"]


def test_each_filter_alone_and_with_paging(data_dir):
    items = made_up_mix()
    c = journeys_client(data_dir, items)
    for params, want in FILTERS:
        expect = [j for j in items[::-1] if want(j)]
        assert 0 < len(expect) < len(items), params
        assert c.get("/journeys", params={**params, "limit": 100}).json() == expect, params
        assert page_all(c, params) == expect, params
    # Together: all filters apply at once; `to` and `before` both apply.
    both = {"with_charge": "true", "place": "b", "to": items[20]["id"]}
    expect = [
        j
        for j in items[::-1]
        if j["charges"] and j["from_place_id"] == "b" and j["id"] <= items[20]["id"]
    ]
    assert page_all(c, both, limit=2) == expect and expect
    got = c.get("/journeys", params={**both, "before": items[20]["id"]}).json()
    assert got == [j for j in expect if j["id"] < items[20]["id"]]
    assert c.get("/journeys", params={"with_charge": "maybe"}).status_code == 422


def test_two_places_with_the_same_name_filter_apart(data_dir):
    items = made_up_mix()
    c = journeys_client(data_dir, items)
    a = c.get("/journeys", params={"place": "a", "limit": 100}).json()
    b = c.get("/journeys", params={"place": "b", "limit": 100}).json()
    assert a and b and {j["from"] for j in a + b} == {"Test Bay"}
    assert {j["from_place_id"] for j in a} == {"a"} and {j["from_place_id"] for j in b} == {"b"}
    assert not {j["id"] for j in a} & {j["id"] for j in b}


def test_days_totals_equal_the_sum_of_the_list(data_dir):
    items = made_up_mix()
    c = journeys_client(data_dir, items)
    for params, _ in [({}, None), *FILTERS]:
        days = c.get("/journeys/days", params=params).json()
        listed = c.get("/journeys", params={**params, "limit": 100}).json()
        assert [d["day"] for d in days] == sorted({d["day"] for d in days}, reverse=True)
        assert sum(d["count"] for d in days) == len(listed), params
        for k in ("km", "kwh_used", "kwh_charged"):
            assert round(sum(d[k] for d in days), 2) == round(sum(j[k] for j in listed), 2)
        cost = [j["cost_eur"] for j in listed if j["cost_eur"] is not None]
        assert round(sum(d["cost_eur"] or 0 for d in days), 2) == round(sum(cost), 2)
    assert any(d["cost_eur"] is None for d in c.get("/journeys/days").json())


def test_days_follow_the_local_time_zone_on_a_dst_day(data_dir):
    # Europe/London moves from GMT to BST at 01:00 UTC on 2026-03-29.
    h = 3_600_000
    t0 = 1_774_742_400_000  # 2026-03-29 00:00 UTC = 00:00 local
    items = [
        item(0, t=t0 - 1),  # 2026-03-28 23:59:59.999 local
        item(1, t=t0),  # 2026-03-29 00:00 local
        item(2, t=t0 + 22 * h + 59 * 60_000),  # 23:59 local (BST)
        item(3, t=t0 + 23 * h),  # 2026-03-30 00:00 local: a 23 h day
    ]
    c = journeys_client(data_dir, items, ZoneInfo("Europe/London"))
    days = c.get("/journeys/days").json()
    assert [(d["day"], d["count"]) for d in days] == [
        ("2026-03-30", 1), ("2026-03-29", 2), ("2026-03-28", 1),
    ]  # fmt: skip
    # The same rows in UTC split differently: 23:00 UTC is still the 29th.
    utc = journeys_client(data_dir, items).get("/journeys/days").json()
    assert [(d["day"], d["count"]) for d in utc] == [("2026-03-29", 3), ("2026-03-28", 1)]
    # A day's range in ms (as the client gets it for a jump) picks the same journeys.
    one = c.get("/journeys/days", params={"from": t0, "to": t0 + 23 * h - 1}).json()
    assert one == [d for d in days if d["day"] == "2026-03-29"]


def test_heating_compare(data_dir):
    import time

    now = int(time.time() * 1000)

    def row(days_ago, c, pre):
        return {
            "start": now - days_ago * 86_400_000,
            "outdoor_c": c,
            "preheat": pre,
            "preheat_kwh": 2.0 if pre else 0.0,
            "battery_kwh": 4.0,
            "eur": 1.0,
            "km": 20.0,
        }

    rows = [row(1, -3.0, True), row(2, -2.0, True), row(3, -1.0, True), row(4, -4.0, False)]
    rows += [row(100, -3.0, False)] * 5  # older than 90 days
    c = CheckedClient(
        create_app(places.store_path(data_dir), TOKEN, UTC, heating_rows=lambda: rows)
    )
    c.headers["Authorization"] = f"Bearer {TOKEN}"
    got = c.get("/heating/compare").json()
    assert [(g["band_c"], g["preheat"], g["count"]) for g in got] == [
        ([-5, 0], False, 1),
        ([-5, 0], True, 3),
    ]
    assert got[0]["battery_kwh"] is None and got[1]["preheat_kwh"] == 2.0
    assert len(c.get("/heating/compare?days=120").json()) == 2
    assert c.get("/heating/compare?days=120").json()[0]["count"] == 6
