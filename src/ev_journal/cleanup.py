"""Delete rows that a recompute no longer produces.

Each run writes all rows at the same timestamps, so a rerun never
duplicates. But when two journeys merge, the second one's old row would
stay. So after writing, the rows that exist but were not produced are
deleted. Usually there are none, so this costs only a few reads.
"""

from ev_journal.influx import Influx
from ev_journal.journeys import Result

# measurement -> a field that every row has
KEY_FIELD = {
    "ev_trips": "end_ts",
    "ev_journeys": "end_ts",
    "ev_outings": "end_ts",
    "ev_month": "km",
}


def produced(r: Result) -> dict[str, set[int]]:
    return {
        "ev_trips": {t.start for t in r.trips},
        "ev_journeys": {j.start for j in r.journeys},
        "ev_outings": {o.start for o in r.outings},
        "ev_month": {m.start for m in r.months},
    }


def stale_statements(reader: Influx, r: Result) -> list[str]:
    # A SELECT without a time bound sees rows up to now. We never write rows
    # in the future, so that is all of them.
    out = []
    for m, keep in produced(r).items():
        rows = reader.query(f'SELECT "{KEY_FIELD[m]}" FROM "{m}"')
        for t in sorted({int(row["time"]) for row in rows} - keep):
            out.append(f'DELETE FROM "{m}" WHERE time = {t}ms')
    keep_tracks = {str(j.id) for j in r.journeys}
    rows = reader.query('SHOW TAG VALUES FROM "ev_track" WITH KEY = "journey"')
    # Journey ids are ms numbers; anything else is not ours to quote into a query.
    for v in sorted({row["value"] for row in rows if row["value"].isdigit()} - keep_tracks):
        out.append(f'DROP SERIES FROM "ev_track" WHERE "journey" = \'{v}\'')
    return out


def cleanup(reader: Influx, writer: Influx, r: Result) -> int:
    """Delete the stale rows. Returns how many statements ran.

    A run with no trips deletes nothing: an empty read must never wipe the history.
    """
    if not r.trips:
        return 0
    stmts = stale_statements(reader, r)
    if stmts:
        writer.execute(stmts)
    return len(stmts)
