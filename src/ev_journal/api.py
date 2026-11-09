"""The HTTP API for the store. See docs/api.md and docs/openapi.json.

Every route needs `Authorization: Bearer <token>`. The token comes from a
file outside the repo (EVJ_API_TOKEN_FILE). Without a token, every request
is refused.
"""

import hmac
import json
import sqlite3
import statistics
import sys
import uuid
from collections.abc import Callable
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, FastAPI, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ev_journal import charges, forecast
from ev_journal import places as store
from ev_journal.geo import dist_m

Kind = Literal["home", "work", "station", "other"]
SUGGEST_RADIUS_M = 100.0
SUGGEST_MIN_VISITS = 3


class Error(BaseModel):
    detail: str


class Score(BaseModel):
    days: int
    covered_pct: float
    normal_error_kwh: float


class Advice(BaseModel):
    """Tomorrow's energy need and how to charge for it (docs/forecast.md)."""

    day: str
    day_type: str  # workday, weekend, holiday or an extra type from the settings
    normal_kwh: float
    bad_kwh: float
    soc_now: float
    target: float
    level: Literal["low", "mid", "high"] | None
    start: str | None
    end: str | None
    departure: str | None
    capped: bool
    preheat_off: bool
    message: str
    score: dict[str, Score]
    soc_arrival: float | None = None  # only with soc or km_home (#27)
    computed_at: str | None = None


class PlaceIn(BaseModel):
    name: str = Field(min_length=1)
    aliases: list[str] = []
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    radius_m: float = Field(default=150, gt=0)
    kind: Kind = "other"
    destination: bool = False
    version: int | None = Field(default=None, description="The version the client saw.")


class Place(BaseModel):
    id: str
    name: str
    aliases: list[str]
    lat: float
    lon: float
    radius_m: float
    kind: Kind
    destination: bool
    version: int
    updated_at: int


class RouteIn(BaseModel):
    name: str = Field(min_length=1)
    place_ids: list[str] = Field(min_length=1)
    version: int | None = Field(default=None, description="The version the client saw.")


class Route(BaseModel):
    id: str
    name: str
    place_ids: list[str]
    version: int
    updated_at: int


class Deleted(BaseModel):
    id: str
    version: int
    updated_at: int


class Written(BaseModel):
    id: str
    version: int
    updated_at: int
    stale: bool = Field(description="The client's version was old; its write still won.")


class PlaceList(BaseModel):
    items: list[Place]
    deleted: list[Deleted]
    cursor: int = Field(description="Send as changed_since next time.")


class RouteList(BaseModel):
    items: list[Route]
    deleted: list[Deleted]
    cursor: int = Field(description="Send as changed_since next time.")


class Suggestion(BaseModel):
    lat: float
    lon: float
    visits: int
    typical_hour: int = Field(description="Median local hour of arrival.")


class LabelIn(BaseModel):
    label: Literal["commute", "other"] | None = None
    not_me: bool = False


class Label(BaseModel):
    journey_id: int
    label: Literal["commute", "other"] | None
    not_me: bool
    updated_at: int


class CommandIn(BaseModel):
    client_id: str = Field(min_length=1, description="Unique per command; a repeat is ignored.")
    time: int
    text: str
    action: str
    place_id: str | None = None
    route_id: str | None = None


class Command(BaseModel):
    client_id: str
    time: int
    text: str
    action: str
    place_id: str | None
    route_id: str | None
    received_at: int


class ReceiptIn(BaseModel):
    amount_eur: float = Field(gt=0)
    network_kwh: float = Field(gt=0, description="The kWh the network reports.")
    time: int | None = Field(default=None, description="Time on the receipt, ms.")


class ChargePrice(BaseModel):
    charge_id: int
    price_eur_per_kwh: float | None = Field(description="EUR per battery kWh.")
    price_source: Literal["receipt", "answer", "estimate"] | None
    network_kwh: float | None
    cost_eur: float | None


class EstimateStation(BaseModel):
    id: str
    name: str
    max_power_kw: float | None


class PriceEstimate(BaseModel):
    station: EstimateStation | None = Field(description="The nearest station within 75 m.")
    match: Literal["ok", "unsure", "no station"]
    eur_per_battery_kwh: float | None
    source: Literal["last_receipt", "feed"] | None
    loss_ratio: float = Field(description="Network kWh per battery kWh.")


class TrackPoint(BaseModel):
    t: int = Field(description="Time of the GPS fix, ms.")
    lat: float
    lon: float
    kwh_per_100km: float | None = Field(
        description="Battery kWh per 100 km of the part since the point before."
        " null for the first point and where the energy counters have a gap."
    )


class Question(BaseModel):
    id: int = Field(description="The charge id (its start, ms).")
    date: str = Field(description="Local date of the charge.")
    station_name: str | None
    battery_kwh: float | None
    opened_at: int


class AnswerIn(BaseModel):
    text: str = Field(description='The amount in EUR and the network kWh, e.g. "5.00 12.5".')


def _place(r: sqlite3.Row) -> Place:
    return Place(
        id=r["id"],
        name=r["name"],
        aliases=json.loads(r["aliases"]),
        lat=r["lat"],
        lon=r["lon"],
        radius_m=r["radius_m"],
        kind=r["kind"],
        destination=bool(r["destination"]),
        version=r["version"],
        updated_at=r["updated_at"],
    )


def _route(r: sqlite3.Row) -> Route:
    return Route(
        id=r["id"],
        name=r["name"],
        place_ids=json.loads(r["place_ids"]),
        version=r["version"],
        updated_at=r["updated_at"],
    )


def suggestions(
    stops: list[tuple[int, float, float]], known: list[store.Place], tz: ZoneInfo
) -> list[Suggestion]:
    """Frequent stops outside every place. stops are (t, lat, lon) of journey ends."""
    free = [s for s in stops if store.place_at(known, (s[1], s[2])) is None]
    near = [
        {j for j, b in enumerate(free) if dist_m(a[1], a[2], b[1], b[2]) <= SUGGEST_RADIUS_M}
        for a in free
    ]
    left = set(range(len(free)))
    out = []
    while left:
        # The stop with the most free neighbours starts the next group.
        centre = max(sorted(left), key=lambda i: len(near[i] & left))
        group = near[centre] & left
        if len(group) < SUGGEST_MIN_VISITS:
            break
        left -= group
        pts = [free[i] for i in sorted(group)]
        hours = [datetime.fromtimestamp(s[0] / 1000, tz).hour for s in pts]
        out.append(
            Suggestion(
                lat=statistics.fmean(s[1] for s in pts),
                lon=statistics.fmean(s[2] for s in pts),
                visits=len(pts),
                typical_hour=int(statistics.median_low(hours)),
            )
        )
    return out


class Fail(Exception):
    """An error answer: {"detail": "..."}."""

    def __init__(self, status: int, detail: str):
        self.status, self.detail = status, detail


@contextmanager
def write_tx(conn: sqlite3.Connection, mode: str = "IMMEDIATE"):
    """One transaction. IMMEDIATE locks the file for writing, so reads and
    writes inside it see no other writer. DEFERRED is a stable view for reads."""
    conn.execute(f"BEGIN {mode}")
    try:
        yield
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def create_app(
    db_path: Path,
    token: str | None,
    tz: ZoneInfo,
    stops: Callable[[], list[tuple[int, float, float]]] = list,
    journeys: Callable[[], list[tuple[int, int]]] = list,
    advice: Callable[[], dict | None] = lambda: None,
    advice_ctx: Callable[[], dict | None] = lambda: None,
    track: Callable[[int], list[dict] | None] = lambda _id: None,
) -> FastAPI:
    """stops gives the (t, lat, lon) of journey ends from the last recompute,
    journeys the (start, end) of each journey, advice the last advice,
    advice_ctx what is needed to make it again on request and track the
    points of a journey (None for an unknown journey)."""

    def db():
        conn = sqlite3.connect(db_path, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    def auth(request: Request):
        scheme, _, got = request.headers.get("authorization", "").partition(" ")
        ok = scheme.lower() == "bearer" and token
        if not ok or not hmac.compare_digest(got.strip().encode(), token.encode()):
            raise Fail(401, "missing or wrong token")

    app = FastAPI(title="ev-journal", version="1")

    @app.exception_handler(Fail)
    def on_fail(_request, e: Fail):
        return JSONResponse({"detail": e.detail}, status_code=e.status)

    @app.exception_handler(RequestValidationError)
    def on_invalid(_request, e: RequestValidationError):
        msgs = [f"{'.'.join(str(x) for x in err['loc'])}: {err['msg']}" for err in e.errors()]
        return JSONResponse({"detail": "; ".join(msgs)}, status_code=422)

    r = APIRouter(
        dependencies=[Depends(auth)],
        responses={401: {"model": Error}, 422: {"model": Error}},
    )
    nf = {404: {"model": Error}}

    # Places and routes share the same sync rules.
    def list_rows(conn, table, conv, changed_since):
        with write_tx(conn, "DEFERRED"):  # one view for the rows and the cursor
            cursor = conn.execute(f"SELECT COALESCE(MAX(seq), 0) FROM {table}").fetchone()[0]
            if changed_since is None:
                rows = list(conn.execute(f"SELECT * FROM {table} WHERE deleted = 0 ORDER BY id"))
            else:
                rows = list(
                    conn.execute(
                        f"SELECT * FROM {table} WHERE seq > ? ORDER BY seq", (changed_since,)
                    )
                )
        items = [conv(x) for x in rows if not x["deleted"]]
        gone = [
            Deleted(id=x["id"], version=x["version"], updated_at=x["updated_at"])
            for x in rows
            if x["deleted"]
        ]
        return items, gone, cursor

    def get_row(conn, table, id_):
        row = conn.execute(f"SELECT * FROM {table} WHERE id = ? AND deleted = 0", (id_,))
        row = row.fetchone()
        if row is None:
            raise Fail(404, "not found")
        return row

    def next_seq(conn, table) -> int:
        return conn.execute(f"SELECT COALESCE(MAX(seq), 0) + 1 FROM {table}").fetchone()[0]

    def save(conn, table, id_, cols, seen: int | None, create: bool) -> Written:
        with write_tx(conn):
            old = conn.execute(f"SELECT version, deleted FROM {table} WHERE id = ?", (id_,))
            old = old.fetchone()
            if (old is None or old["deleted"]) and not create:
                raise Fail(404, "not found")
            if callable(cols):
                cols = cols()
            version = (old["version"] if old else 0) + 1
            stale = old is not None and seen is not None and seen < old["version"]
            t = store.now_ms()
            cols = {
                **cols,
                "id": id_,
                "version": version,
                "updated_at": t,
                "deleted": 0,
                "seq": next_seq(conn, table),
            }
            names = ", ".join(cols)
            marks = ", ".join("?" for _ in cols)
            conn.execute(
                f"INSERT OR REPLACE INTO {table} ({names}) VALUES ({marks})", list(cols.values())
            )
        return Written(id=id_, version=version, updated_at=t, stale=stale)

    def remove(conn, table, id_, seen: int | None, check=None) -> Written:
        with write_tx(conn):
            old = get_row(conn, table, id_)
            if check:
                check()
            t = store.now_ms()
            conn.execute(
                f"UPDATE {table} SET deleted = 1, version = ?, updated_at = ?, seq = ?"
                " WHERE id = ?",
                (old["version"] + 1, t, next_seq(conn, table), id_),
            )
        stale = seen is not None and seen < old["version"]
        return Written(id=id_, version=old["version"] + 1, updated_at=t, stale=stale)

    def place_cols(p: PlaceIn) -> dict:
        return {
            "name": p.name,
            "aliases": json.dumps(p.aliases),
            "lat": p.lat,
            "lon": p.lon,
            "radius_m": p.radius_m,
            "kind": p.kind,
            "destination": int(p.destination),
        }

    def route_cols(conn, p: RouteIn) -> dict:
        ids = p.place_ids
        marks = ",".join("?" * len(ids))
        known = {
            x["id"]
            for x in conn.execute(
                f"SELECT id FROM places WHERE deleted = 0 AND id IN ({marks})", ids
            )
        }
        if missing := [i for i in ids if i not in known]:
            raise Fail(422, f"unknown place ids: {missing}")
        return {"name": p.name, "place_ids": json.dumps(ids)}

    def not_in_a_route(conn, place_id):
        for x in conn.execute("SELECT id, place_ids FROM routes WHERE deleted = 0"):
            if place_id in json.loads(x["place_ids"]):
                raise Fail(409, f"route {x['id']} uses this place")

    @r.get("/places", response_model=PlaceList)
    def list_places(changed_since: int | None = None, conn=Depends(db)):
        items, gone, cursor = list_rows(conn, "places", _place, changed_since)
        return PlaceList(items=items, deleted=gone, cursor=cursor)

    @r.get("/places/suggestions", response_model=list[Suggestion])
    def place_suggestions(conn=Depends(db)):
        return suggestions(stops(), store.all_places(conn), tz)

    @r.post("/places", response_model=Written, status_code=201)
    def create_place(p: PlaceIn, conn=Depends(db)):
        return save(conn, "places", uuid.uuid4().hex[:12], place_cols(p), None, True)

    @r.get("/places/{place_id}", response_model=Place, responses=nf)
    def get_place(place_id: str, conn=Depends(db)):
        return _place(get_row(conn, "places", place_id))

    @r.put("/places/{place_id}", response_model=Written, responses=nf)
    def put_place(place_id: str, p: PlaceIn, conn=Depends(db)):
        return save(conn, "places", place_id, place_cols(p), p.version, False)

    @r.delete(
        "/places/{place_id}",
        response_model=Written,
        responses={**nf, 409: {"model": Error, "description": "A route uses this place."}},
    )
    def delete_place(place_id: str, version: int | None = None, conn=Depends(db)):
        return remove(conn, "places", place_id, version, lambda: not_in_a_route(conn, place_id))

    @r.get("/routes", response_model=RouteList)
    def list_routes(changed_since: int | None = None, conn=Depends(db)):
        items, gone, cursor = list_rows(conn, "routes", _route, changed_since)
        return RouteList(items=items, deleted=gone, cursor=cursor)

    @r.post("/routes", response_model=Written, status_code=201)
    def create_route(p: RouteIn, conn=Depends(db)):
        rid = uuid.uuid4().hex[:12]
        return save(conn, "routes", rid, lambda: route_cols(conn, p), None, True)

    @r.get("/routes/{route_id}", response_model=Route, responses=nf)
    def get_route(route_id: str, conn=Depends(db)):
        return _route(get_row(conn, "routes", route_id))

    @r.put("/routes/{route_id}", response_model=Written, responses=nf)
    def put_route(route_id: str, p: RouteIn, conn=Depends(db)):
        return save(conn, "routes", route_id, lambda: route_cols(conn, p), p.version, False)

    @r.delete("/routes/{route_id}", response_model=Written, responses=nf)
    def delete_route(route_id: str, version: int | None = None, conn=Depends(db)):
        return remove(conn, "routes", route_id, version)

    @r.put(
        "/journeys/{journey_id}/label",
        response_model=Label,
        responses={404: {"model": Error, "description": "No journey holds this time."}},
    )
    def put_label(journey_id: int, p: LabelIn, conn=Depends(db)):
        """Set the label and "not me" of the journey that contains this time.

        The journey id is its start time (ms). A recompute keeps the label.
        """
        if not any(a <= journey_id <= b for a, b in journeys()):
            raise Fail(404, "no journey holds this time (yet)")
        t = store.now_ms()
        with write_tx(conn):
            conn.execute(
                "INSERT OR REPLACE INTO labels VALUES (?, ?, ?, ?)",
                (journey_id, p.label, int(p.not_me), t),
            )
        return Label(journey_id=journey_id, label=p.label, not_me=p.not_me, updated_at=t)

    @r.get("/journeys/{journey_id}/track", response_model=list[TrackPoint], responses=nf)
    def get_track(journey_id: int):
        """The journey's real GPS fixes, one per 50 m like ev_track, for a map.

        The journey id is its start time (ms).
        """
        pts = track(journey_id)
        if pts is None:
            raise Fail(404, "no such journey")
        return pts

    @r.post(
        "/commands",
        response_model=Command,
        status_code=201,
        responses={200: {"model": Command, "description": "A repeat; ignored."}},
    )
    def post_command(c: CommandIn, response: Response, conn=Depends(db)):
        with write_tx(conn):
            cur = conn.execute(
                "INSERT OR IGNORE INTO commands VALUES (?, ?, ?, ?, ?, ?, ?)",
                (c.client_id, c.time, c.text, c.action, c.place_id, c.route_id, store.now_ms()),
            )
            row = conn.execute("SELECT * FROM commands WHERE client_id = ?", (c.client_id,))
            row = row.fetchone()
        if cur.rowcount == 0:
            response.status_code = 200
        return Command(**dict(row))

    def charge_price(conn, cid: int) -> ChargePrice:
        row = conn.execute(
            "SELECT id, price_eur_per_kwh, price_source, network_kwh, cost_eur"
            " FROM charges WHERE id = ?",
            (cid,),
        ).fetchone()
        return ChargePrice(
            charge_id=row[0],
            price_eur_per_kwh=row[1],
            price_source=row[2],
            network_kwh=row[3],
            cost_eur=row[4],
        )

    @r.post("/charges/{charge_id}/receipt", response_model=ChargePrice, responses=nf)
    def post_receipt(charge_id: int, p: ReceiptIn, conn=Depends(db)):
        """A receipt for a charge. It replaces an answer or an estimate."""
        with write_tx(conn):
            if not conn.execute("SELECT 1 FROM charges WHERE id = ?", (charge_id,)).fetchone():
                raise Fail(404, "no such charge")
            conn.execute(
                "INSERT OR REPLACE INTO receipts VALUES (?, ?, ?, ?, ?)",
                (charge_id, p.amount_eur, p.network_kwh, p.time, store.now_ms()),
            )
            charges.price(conn, charge_id)
        return charge_price(conn, charge_id)

    @r.get("/charges/price-estimate", response_model=PriceEstimate)
    def price_estimate(
        lat: float = Query(ge=-90, le=90),
        lon: float = Query(ge=-180, le=180),
        kind: Literal["dc", "ac"] = Query(),
        conn=Depends(db),
    ):
        """The price per battery kWh for a charge here, for the car app's charge screen."""
        return charges.estimate_at(conn, (lat, lon), kind)

    @r.get("/questions", response_model=list[Question])
    def list_questions(conn=Depends(db)):
        """Open questions: away charges with no receipt and no answer."""
        rows = conn.execute(
            "SELECT q.charge_id, s.name, q.battery_kwh, q.opened_at FROM questions q"
            " JOIN charges c ON c.id = q.charge_id AND c.at_home = 0"
            " LEFT JOIN stations s ON s.id = c.station_id"
            " WHERE q.answered_at IS NULL"
            " AND q.charge_id NOT IN (SELECT charge_id FROM receipts)"
            " ORDER BY q.charge_id"
        )
        return [
            Question(
                id=q[0],
                date=datetime.fromtimestamp(q[0] / 1000, tz).date().isoformat(),
                station_name=q[1],
                battery_kwh=q[2],
                opened_at=q[3],
            )
            for q in rows
        ]

    @r.post("/questions/{question_id}/answer", response_model=ChargePrice, responses=nf)
    def answer(question_id: int, p: AnswerIn, conn=Depends(db)):
        """The owner's answer, plain: "<amount EUR> <network kWh>"."""
        try:
            amount, kwh = charges.parse_answer(p.text)
        except ValueError as e:
            raise Fail(422, str(e)) from e
        with write_tx(conn):
            q = conn.execute("SELECT 1 FROM questions WHERE charge_id = ?", (question_id,))
            if not q.fetchone():
                raise Fail(404, "no such question")
            conn.execute(
                "UPDATE questions SET answered_at = ?, amount_eur = ?, network_kwh = ?"
                " WHERE charge_id = ?",
                (store.now_ms(), amount, kwh, question_id),
            )
            charges.price(conn, question_id)
        return charge_price(conn, question_id)

    @r.get("/advice", response_model=Advice, responses=nf)
    def get_advice(
        soc: float | None = Query(None, ge=0, le=100),
        km_home: float | None = Query(None, ge=0),
    ):
        if soc is not None or km_home is not None:
            ctx = advice_ctx()  # one read: the advice and its inputs match
            if ctx is None:
                raise Fail(404, "no advice yet")
            return forecast.on_request(ctx, soc, km_home, datetime.now(tz))
        a = advice()
        if a is None:
            raise Fail(404, "no advice yet")
        return a

    app.include_router(r)
    return app


def openapi() -> dict:
    return create_app(Path("/nonexistent"), None, ZoneInfo("UTC")).openapi()


if __name__ == "__main__":
    # python -m ev_journal.api > docs/openapi.json
    json.dump(openapi(), sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
