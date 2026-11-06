import sqlite3

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from driveguard.risk.scoring import FactorValue, compute_factors, explain, load_weights, score
from driveguard.store import queries as q


def _trip_inputs(db, trip_id):
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    trip = q.get_trip(conn, trip_id)
    return q.trip_stored_events(conn, trip_id), q.list_incidents(conn, trip_id), trip["distance_km"]


def _trips_with(sim, kind):
    return sorted({l.trip_id for l in sim.labels if l.kind == kind})


def test_fixture_coverage(golden):
    from driveguard.sim.generator import INCIDENT_KINDS
    _, sim = golden
    kinds = {l.kind for l in sim.labels}
    assert set(INCIDENT_KINDS) <= kinds and {"pothole", "firm_brake"} & kinds
    assert sum(e.type == "driver_note" for e in sim.events) >= 2


def test_weights_sum_to_100():
    assert sum(f["weight"] for f in load_weights()["factors"].values()) == 100


def test_contributions_sum_to_score_and_are_deterministic(golden):
    db, sim = golden
    for trip_id in sorted({str(e.trip_id) for e in sim.events}):
        events, incidents, km = _trip_inputs(db, trip_id)
        a = score(trip_id, compute_factors(events, incidents, km))
        b = score(trip_id, compute_factors(events, incidents, km))
        assert a == b
        assert 0 <= a.score <= 100
        assert a.score == pytest.approx(sum(f.contribution for f in a.factors), abs=1e-6)
        trip_event_ids = {e.event_id for e in events}
        for f in a.factors:
            assert set(f.evidence_event_ids) <= trip_event_ids
            assert 0 <= f.normalized <= 1


def test_crash_trip_scores_crash_factor(golden):
    db, sim = golden
    trips = _trips_with(sim, "crash")
    assert trips, "fixture should contain a crash"
    events, incidents, km = _trip_inputs(db, trips[0])
    crash = next(f for f in score(trips[0], compute_factors(events, incidents, km)).factors if f.name == "crash")
    assert crash.raw_value == 1 and crash.contribution == 20 and crash.evidence_event_ids


def test_phone_trip_scores_phone_factor(golden):
    db, sim = golden
    trip = _trips_with(sim, "phone_distraction")[0]
    events, incidents, km = _trip_inputs(db, trip)
    phone = next(f for f in score(trip, compute_factors(events, incidents, km)).factors if f.name == "phone_moving_s")
    assert phone.raw_value >= 10 and phone.contribution > 0 and phone.evidence_event_ids


def test_score_id_changes_when_inputs_change(golden):
    db, sim = golden
    trip = sorted({str(e.trip_id) for e in sim.events})[0]
    events, incidents, km = _trip_inputs(db, trip)
    a = score(trip, compute_factors(events, incidents, km))
    b = score(trip, compute_factors(events[:-50], incidents, km))
    assert a.score_id != b.score_id


def test_explanation_has_one_line_per_factor_sorted(golden):
    db, sim = golden
    trip = _trips_with(sim, "crash")[0]
    events, incidents, km = _trip_inputs(db, trip)
    rs = score(trip, compute_factors(events, incidents, km))
    lines = explain(rs)
    assert len(lines) == len(rs.factors) and lines[0].startswith("crash:")


_NAMES = list(load_weights()["factors"])


@settings(max_examples=200, deadline=None)
@given(st.lists(st.floats(min_value=-1e6, max_value=1e6, allow_nan=False), min_size=len(_NAMES),
                max_size=len(_NAMES)))
def test_score_bounded_and_additive_for_any_raw_values(raws):
    meta = load_weights()["factors"]
    factors = [FactorValue(name=n, raw_value=r, unit=meta[n]["unit"], description="", evidence_event_ids=[])
               for n, r in zip(_NAMES, raws)]
    rs = score("t", factors)
    assert 0 <= rs.score <= 100
    assert rs.score == pytest.approx(sum(f.contribution for f in rs.factors), abs=1e-5)


def test_missing_factor_is_rejected():
    meta = load_weights()["factors"]
    factors = [FactorValue(name=n, raw_value=0, unit=meta[n]["unit"], description="", evidence_event_ids=[])
               for n in _NAMES[:-1]]
    with pytest.raises(ValueError):
        score("t", factors)
