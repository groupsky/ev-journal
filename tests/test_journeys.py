from zoneinfo import ZoneInfo

import pytest
from week import HOME, LAKE, PLACES, STATION, WORK, week

from ev_journal.geo import dist_m, thin, track_km
from ev_journal.journeys import build
from ev_journal.places import Place, place_at

UTC = ZoneInfo("UTC")


@pytest.fixture
def r():
    w = week()
    return build(w.trips, w.charges, w.fixes, PLACES, UTC)


def ends(j):
    return (j.start_place.id, j.end_place.id)


def test_journeys_of_the_week(r):
    assert [ends(j) for j in r.journeys] == [
        (HOME.id, WORK.id),
        (WORK.id, HOME.id),
        (HOME.id, LAKE.id),
        (LAKE.id, HOME.id),
        (HOME.id, LAKE.id),
        (LAKE.id, HOME.id),
        (HOME.id, WORK.id),
        (WORK.id, HOME.id),
        (HOME.id, STATION.id),
        (HOME.id, WORK.id),
    ]


def test_stop_under_30_min_is_joined(r):
    j = r.journeys[2]  # Tue: home -> station -> lake
    assert len(j.trips) == 2
    assert j.waiting_min == 10
    assert j.charges == []


def test_stop_over_30_min_splits(r):
    # Mon at work 9 h; Tue at the lake 3 h.
    assert r.journeys[0].end_place == WORK and r.journeys[1].start_place == WORK
    assert r.journeys[2].end_place == LAKE and r.journeys[3].start_place == LAKE


def test_dc_charge_on_the_way_stays_inside(r):
    j = r.journeys[4]  # Wed: 40 min charge + 15 min coffee
    assert len(j.trips) == 2
    assert len(j.charges) == 1
    assert j.charge_min == 40
    assert j.waiting_min == 15
    assert j.kwh_charged == 30.0


def test_destination_charge_ends_the_journey(r):
    j = r.journeys[4]
    assert j.end_place == LAKE
    assert j.end == j.trips[-1].end  # ends on arrival
    assert 10.0 not in [c.kwh for c in j.charges]  # the lake charge is not "on the way"
    assert r.journeys[5].charges == []


def test_home_charge_ends_the_outing(r):
    # Thu: a 20 min stop at home with a charge still splits.
    assert ends(r.journeys[7]) == (WORK.id, HOME.id)
    assert ends(r.journeys[8]) == (HOME.id, STATION.id)
    # Outings: Mon, Tue, Wed, Thu. The last two journeys never come home.
    assert len(r.outings) == 4
    assert [len(o.journeys) for o in r.outings] == [2, 2, 2, 2]


def test_commute_label(r):
    labels = [j.label for j in r.journeys]
    assert labels[0] == labels[1] == "commute"  # Mon
    assert labels[6] == labels[7] == "commute"  # Thu
    assert labels[2] == "other"
    assert labels[9] == "other"  # Sat, home -> work


def test_distance_from_gps(r):
    t = r.trips[0]
    straight = dist_m(HOME.lat, HOME.lon, WORK.lat, WORK.lon) / 1000
    assert t.km_source == "gps"
    assert t.km == pytest.approx(straight, rel=0.01)


def test_month(r):
    (m,) = r.months
    assert m.key == "2026-01"
    assert m.trips == 12
    assert m.journeys == 10
    assert m.outings == 4
    assert m.kwh_home == 23.0  # Mon 20 + Thu 3
    assert m.kwh_away == 40.0  # Wed 30 + 10
    assert m.commute_km == pytest.approx(sum(j.km for j in r.journeys if j.label == "commute"))


def test_odometer_gap():
    w = week()
    km = sum(
        track_km(w.trips and [f for f in w.fixes if t.start <= f[0] <= t.end]) for t in w.trips
    )
    odo = [(w.trips[0].start - 1, 1000.0), (w.trips[-1].end + 1, 1000.0 + round(km))]
    r = build(w.trips, w.charges, w.fixes, PLACES, UTC, odometer=odo)
    assert r.months[0].odo_km == round(km)
    assert abs(r.months[0].gap_pct) < 1


def test_trip_without_gps_uses_homy_km():
    w = week()
    w.trips[0].km_homy = 12.0
    fixes = [f for f in w.fixes if not w.trips[0].start <= f[0] <= w.trips[0].end]
    r = build(w.trips, w.charges, fixes, PLACES, UTC)
    assert r.trips[0].km_source == "homy" and r.trips[0].km == 12.0


def test_thin_keeps_ends_and_spacing():
    fixes = [(i, 0.0, i * 0.0001) for i in range(100)]  # about 11 m apart
    kept = thin(fixes, 50)
    assert kept[0] == fixes[0] and kept[-1] == fixes[-1]
    assert all(dist_m(a[1], a[2], b[1], b[2]) >= 50 for a, b in zip(kept, kept[1:-1], strict=False))


def test_place_at():
    assert place_at(PLACES, (0.1001, 0.1001)) == HOME
    assert place_at(PLACES, (0.15, 0.15)) is None
    assert place_at(PLACES, None) is None
    # A home place that holds the fix wins over a nearer place of another kind (#28).
    near = Place("p-near", "Test Near", "other", 0.1003, 0.1, 150)
    assert place_at([near, HOME], (0.1002, 0.1)) == HOME
    assert place_at([near, WORK], (0.1002, 0.1)) == near


def test_trip_without_fixes_has_no_places():
    # An old fix from before the trip must not give it a place.
    w = week()
    t = w.trips[1]  # Mon, work -> home
    fixes = [f for f in w.fixes if not t.start <= f[0] <= t.end]
    r = build(w.trips, w.charges, fixes, PLACES, UTC)
    assert r.trips[1].start_place is None and r.trips[1].end_place is None
    # The home charge after it has no known place either.
    assert r.months[0].kwh_home == 3.0
    assert r.months[0].kwh_unknown == 20.0
