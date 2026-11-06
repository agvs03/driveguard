import math
import re
from collections import defaultdict

import pytest

from driveguard.events import parse_event
from driveguard.sim.generator import INCIDENT_KINDS, SimConfig, simulate

BIG = SimConfig(seed=3, n_trips=60, n_drivers=10)


@pytest.fixture(scope="module")
def big():
    return simulate(BIG)


def test_deterministic_for_same_seed():
    a, b = simulate(SimConfig(seed=5, n_trips=5)), simulate(SimConfig(seed=5, n_trips=5))
    assert [e.to_json() for e in a.events] == [e.to_json() for e in b.events]
    assert a.labels == b.labels and a.drivers == b.drivers


def test_different_seed_differs():
    a, b = simulate(SimConfig(seed=5, n_trips=3)), simulate(SimConfig(seed=6, n_trips=3))
    assert [e.to_json() for e in a.events] != [e.to_json() for e in b.events]


def test_trip_structure(big):
    by_trip = defaultdict(list)
    for e in big.events:
        by_trip[e.trip_id].append(e)
    assert len(by_trip) == BIG.n_trips
    for evs in by_trip.values():
        assert [e.seq for e in evs] == list(range(len(evs)))
        assert all(evs[i].event_time_ms <= evs[i + 1].event_time_ms for i in range(len(evs) - 1))
        assert evs[0].type == "trip_start" and evs[-1].type == "trip_end"
        assert evs[-1].payload["last_seq"] == evs[-1].seq
    assert len({e.event_id for e in big.events}) == len(big.events)


def test_events_survive_wire_round_trip(big):
    for e in big.events[:2000]:
        assert parse_event(e.to_json()) == e


def test_all_data_is_marked_synthetic(big):
    assert all(d.synthetic for d in big.drivers)
    assert all(re.fullmatch(r"\+1-555-01\d\d", d.phone) for d in big.drivers)  # reserved fictional range


def test_every_incident_kind_is_generated(big):
    kinds = {l.kind for l in big.labels}
    assert set(INCIDENT_KINDS) <= kinds
    assert {"pothole", "firm_brake", "parked_phone"} <= kinds


def test_label_evidence_exists_and_belongs_to_trip(big):
    by_id = {str(e.event_id): e for e in big.events}
    for lab in big.labels:
        assert lab.evidence_event_ids, lab
        for eid in lab.evidence_event_ids:
            assert str(by_id[eid].trip_id) == lab.trip_id


def test_incident_signatures(big):
    """Each injected incident leaves the documented physical signature."""
    by_id = {str(e.event_id): e for e in big.events}
    by_trip = defaultdict(list)
    for e in big.events:
        by_trip[str(e.trip_id)].append(e)
    for lab in big.labels:
        ev = [by_id[i] for i in lab.evidence_event_ids]
        acc = [e.payload for e in ev if e.type == "accel"]
        if lab.kind == "hard_braking":
            assert min(a["ax_peak"] for a in acc) <= -0.45
        elif lab.kind == "near_miss":
            assert min(a["ax_peak"] for a in acc) <= -0.45
            assert max(abs(a["ay_peak"]) for a in acc) >= 0.45
        elif lab.kind == "crash":
            assert max(math.sqrt(a["ax_peak"] ** 2 + a["ay_peak"] ** 2 + a["az_peak"] ** 2) for a in acc) >= 4.0
            after = [e for e in by_trip[lab.trip_id] if e.type == "gps" and e.event_time_ms > lab.t_end_ms + 1000]
            assert after and all(e.payload["speed_mps"] == 0.0 for e in after)
        elif lab.kind == "phone_distraction":
            moving = [e for e in by_trip[lab.trip_id] if e.type == "gps"
                      and lab.t_start_ms <= e.event_time_ms <= lab.t_end_ms and e.payload["speed_mps"] > 5]
            assert len(moving) >= 10
        elif lab.kind == "parked_phone":
            gps = [e for e in by_trip[lab.trip_id] if e.type == "gps"
                   and lab.t_start_ms <= e.event_time_ms <= lab.t_end_ms]
            assert all(e.payload["speed_mps"] == 0.0 for e in gps)
