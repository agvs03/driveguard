import random

from driveguard.evals.detector_eval import evaluate_detector
from driveguard.pipeline.detector import StoredEvent, detect
from driveguard.sim.generator import INCIDENT_KINDS, SimConfig, simulate

TRIP = "trip-1"


class _B:
    """Tiny builder for hand-made trips: 1 gps + 1 accel per second."""

    def __init__(self, seconds=60, speed=15.0):
        self.ev: list[StoredEvent] = []
        self.seq = 0
        self.speed = [speed] * seconds
        self.acc = [{"ax_mean": 0.0, "ax_peak": 0.0, "ay_peak": 0.0, "az_peak": 1.0} for _ in range(seconds)]
        self.phone: list[tuple[int, str]] = []

    def build(self):
        out = []
        for t in range(len(self.speed)):
            out.append(("gps", t * 1000, {"lat": 0, "lon": 0, "speed_mps": self.speed[t],
                                          "speed_limit_mps": 20, "heading_deg": 0}))
            out.append(("accel", t * 1000 + 500, self.acc[t]))
        out += [("phone", t * 1000 + 200, {"action": a}) for t, a in self.phone]
        out.sort(key=lambda x: x[1])
        return [StoredEvent(f"e{i}", i, tm, typ, p) for i, (typ, tm, p) in enumerate(out)]


def kinds(evs):
    return [i.kind for i in detect(TRIP, evs)]


def test_quiet_trip_has_no_incidents():
    assert kinds(_B().build()) == []


def test_hard_braking_grouped_into_one_incident():
    b = _B()
    b.acc[10]["ax_peak"] = -0.55
    b.acc[11]["ax_peak"] = -0.5
    [inc] = detect(TRIP, b.build())
    assert inc.kind == "hard_braking" and len(inc.trigger_event_ids) == 2
    assert inc.details["max_decel_g"] == 0.55


def test_brake_plus_swerve_is_near_miss():
    b = _B()
    b.acc[20]["ax_peak"] = -0.5
    b.acc[21]["ay_peak"] = -0.5
    assert kinds(b.build()) == ["near_miss"]


def test_braking_next_to_crash_is_absorbed_into_crash():
    b = _B()
    b.acc[30]["ax_peak"] = -0.6
    b.acc[31].update(ax_peak=-6.0, az_peak=2.0)
    assert kinds(b.build()) == ["crash"]


def test_pothole_spike_above_threshold_is_a_known_false_positive():
    b = _B()
    b.acc[12]["az_peak"] = 3.2
    assert kinds(b.build()) == ["crash"]  # documented weakness; agents must reject it


def test_phone_use_while_moving_vs_parked():
    moving = _B()
    moving.phone = [(5, "unlock"), (5, "screen_on"), (25, "screen_off")]
    assert kinds(moving.build()) == ["phone_distraction"]
    parked = _B(speed=0.0)
    parked.phone = [(5, "unlock"), (5, "screen_on"), (25, "screen_off")]
    assert kinds(parked.build()) == []
    short = _B()
    short.phone = [(5, "screen_on"), (9, "screen_off")]
    assert kinds(short.build()) == []


def test_detection_is_independent_of_input_order():
    b = _B()
    b.acc[10]["ax_peak"] = -0.55
    b.acc[40]["ay_peak"] = 0.5
    b.acc[41]["ax_peak"] = -0.5
    b.phone = [(2, "unlock"), (2, "screen_on"), (30, "screen_off")]
    evs = b.build()
    shuffled = evs[:]
    random.Random(0).shuffle(shuffled)
    assert [i.__dict__ for i in detect(TRIP, evs)] == [i.__dict__ for i in detect(TRIP, shuffled)]


def test_detector_against_simulator_ground_truth():
    out = simulate(SimConfig(seed=21, n_trips=60, n_drivers=10))
    by_trip: dict[str, list[StoredEvent]] = {}
    for e in out.events:
        by_trip.setdefault(str(e.trip_id), []).append(
            StoredEvent(str(e.event_id), e.seq, e.event_time_ms, e.type, e.payload))
    incidents = [i.__dict__ for tid, evs in by_trip.items() for i in detect(tid, evs)]
    report = evaluate_detector(out.labels, incidents)
    for k in INCIDENT_KINDS:
        assert report["per_kind"][k]["recall"] == 1.0, (k, report)
    # Every false positive must come from a planted hard negative, never from normal driving.
    assert all(not src.endswith("<-unlabeled") for src in report["false_positive_sources"]), report
