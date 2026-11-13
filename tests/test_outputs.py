import json
from zoneinfo import ZoneInfo

from week import PLACES, week

from ev_journal import outputs
from ev_journal.influx import line
from ev_journal.journeys import build

UTC = ZoneInfo("UTC")


def result():
    w = week()
    return build(w.trips, w.charges, w.fixes, PLACES, UTC)


def test_line_protocol():
    assert (
        line("m", {"journey": "a b"}, {"x": 1, "s": 'say "hi"', "ok": True, "n": None}, 5)
        == 'm,journey=a\\ b x=1.0,s="say \\"hi\\"",ok=true 5'
    )


def test_rerun_writes_the_same_rows():
    # Same timestamps on every run, so InfluxDB overwrites and never duplicates.
    assert outputs.influx_lines(result()) == outputs.influx_lines(result())


def test_rows_per_measurement():
    lines = outputs.influx_lines(result())
    count = {
        m: sum(ln.startswith(m + " ") or ln.startswith(m + ",") for ln in lines)
        for m in ["ev_trips", "ev_journeys", "ev_outings", "ev_month", "ev_track"]
    }
    assert count["ev_trips"] == 12
    assert count["ev_journeys"] == 10
    assert count["ev_outings"] == 4
    assert count["ev_month"] == 1
    assert count["ev_track"] > 0
    track = [ln for ln in lines if ln.startswith("ev_track,")]
    assert len({ln.split(" ")[0] for ln in track}) == 10  # one series per journey


def test_summary():
    r = result()
    s = outputs.summary(r, r.journeys[-1].end, UTC)
    json.dumps(s)
    assert len(s["journeys"]) == 10
    assert s["journeys"][0]["id"] == r.journeys[-1].id  # newest first
    assert s["month"]["month"] == "2026-01"
    assert s["month"]["journeys"] == 10
    assert s["upkeep"] == []
    part = [{"name": "Test item", "status": "soon"}]
    assert outputs.summary(r, r.journeys[-1].end, UTC, part)["upkeep"] == part
    assert s["tyres"] == {"fitted": None, "hint": None, "age_warning": []}
    ty = {"fitted": {"name": "Test set"}, "hint": "winter", "age_warning": []}
    assert outputs.summary(r, r.journeys[-1].end, UTC, None, ty)["tyres"] == ty


def test_preheat_outputs():
    from ev_journal.heating import Preheat

    r = result()
    j = r.journeys[-1]
    j.preheat_kwh, j.preheat_eur = 2.0, 0.2
    r.months[-1].preheat_kwh, r.months[-1].preheat_eur = 2.0, 0.2
    r.preheats = [
        Preheat(
            j.start - 1_800_000,
            j.start - 600_000,
            "state",
            2.0,
            0.1,
            -3.0,
            "night",
            j.id,
            None,
            0.2,
        )
    ]
    lines = outputs.influx_lines(r)
    pre = [ln for ln in lines if ln.startswith("ev_preheat,")]
    assert len(pre) == 1 and "source=state" in pre[0] and "wall_kwh=2.0" in pre[0]
    assert pre[0].endswith(str(j.start - 1_800_000))
    assert any(ln.startswith("ev_journeys ") and "preheat_eur=0.2" in ln for ln in lines)
    assert any(ln.startswith("ev_month ") and "preheat_kwh=2.0" in ln for ln in lines)
    s = outputs.summary(r, j.end, UTC)
    assert s["preheat"]["wall_kwh"] == 2.0 and s["preheat"]["cost_eur"] == 0.2
    assert s["month"]["preheat_eur"] == 0.2
    r.preheats = []
    assert outputs.summary(r, j.end, UTC)["preheat"] is None
