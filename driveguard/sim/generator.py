"""Synthetic trip simulator with labeled incident injection.

Everything here is invented: drivers, vehicles, the city, the coordinates and
phone numbers (555-01xx is a reserved fictional range). Output is fully
determined by `SimConfig` (including the seed).

Physics model (per 1 s step): a driver tracks `speed_limit * speed_factor`
with personality-dependent acceleration limits and stops at some segment
boundaries ("red lights"). Incidents are injected as *signal signatures*
(forced decelerations, accelerometer spikes, phone actions), never as flags,
so the downstream detector has to find them from signals alone.
"""

from __future__ import annotations

import math
import random
import uuid
from dataclasses import dataclass, field
from typing import Any

from driveguard.events import G, Event

INCIDENT_KINDS = ("hard_braking", "crash", "near_miss", "phone_distraction")
NEGATIVE_KINDS = ("pothole", "firm_brake", "parked_phone")
LIMITS_MPS = (13.9, 19.4, 27.8)  # 50 / 70 / 100 km/h

_FIRST = ["Avery", "Jordan", "Riley", "Casey", "Morgan", "Quinn", "Rowan", "Sage",
          "Emerson", "Harper", "Kai", "Logan", "Reese", "Drew", "Parker", "Skyler"]
_LAST = ["Lin", "Okafor", "Novak", "Silva", "Moreau", "Tanaka", "Brennan", "Haddad",
         "Kowalski", "Ferreira", "Lindqvist", "Mbeki", "Duarte", "Iyer"]
_VEHICLES = ["Fleet Hatchback A", "Fleet Sedan B", "Fleet Van C", "Fleet Pickup D"]
_NOTES = [
    "Brakes felt a bit soft this morning.",
    "Heavy rain on the ring road, visibility was poor.",
    "A cyclist cut in front of me near the market square.",
    "Traffic was bad, delivery ran late.",
    "Phone mount came loose, fixed it at a red light.",
    "Nothing unusual today.",
    "Car ahead stopped suddenly without signalling.",
    "Hit a pothole on Elm Street, felt a jolt.",
]

PERSONALITIES: dict[str, dict[str, Any]] = {
    "cautious": {"speed_factor": 0.92, "accel": 1.6, "stop_decel": (1.5, 2.2)},
    "normal": {"speed_factor": 1.00, "accel": 2.0, "stop_decel": (1.8, 2.6)},
    "aggressive": {"speed_factor": 1.10, "accel": 2.6, "stop_decel": (2.3, 2.9)},
}


@dataclass(frozen=True)
class SimConfig:
    seed: int = 7
    n_drivers: int = 8
    n_trips: int = 20
    trip_seconds: tuple[int, int] = (360, 900)
    incident_rate: float = 0.7  # P(trip has >= 1 incident)
    incident_weights: tuple[float, float, float, float] = (0.35, 0.15, 0.25, 0.25)
    negative_rate: float = 0.4  # P(trip has a hard negative)
    note_rate: float = 0.3
    start_epoch_ms: int = 1_767_600_000_000  # 2026-01-05T08:00:00Z
    center: tuple[float, float] = (40.0, -100.0)  # synthetic city centre


@dataclass(frozen=True)
class Driver:
    driver_id: str
    display_name: str
    phone: str
    personality: str
    synthetic: bool = True


@dataclass(frozen=True)
class Vehicle:
    vehicle_id: str
    driver_id: str
    make_model: str


@dataclass(frozen=True)
class Label:
    """Ground truth. Only the eval harness may read these."""

    label_id: str
    trip_id: str
    kind: str
    is_incident: bool
    t_start_ms: int
    t_end_ms: int
    evidence_event_ids: tuple[str, ...]


@dataclass
class SimOutput:
    drivers: list[Driver]
    vehicles: list[Vehicle]
    events: list[Event]
    labels: list[Label]


def _uuid(rng: random.Random) -> uuid.UUID:
    return uuid.UUID(int=rng.getrandbits(128), version=4)


def _hex8(rng: random.Random) -> str:
    return f"{rng.getrandbits(32):08x}"


# --------------------------------------------------------------------------- #
# Incident step programs. Each step is one second; keys:
#   decel_g  forced mean deceleration      crash   v -> 0 with a huge spike
#   ay       lateral peak override         az      vertical peak override
#   brake    brake pressure event          phone   list of phone actions
# --------------------------------------------------------------------------- #

_MIN_SPEED = {"hard_braking": 12.0, "near_miss": 12.0, "crash": 12.0,
              "firm_brake": 10.0, "phone_distraction": 8.0, "pothole": 5.0}


def _steps_for(kind: str, rng: random.Random) -> list[dict[str, Any]]:
    sign = rng.choice((-1.0, 1.0))
    if kind == "hard_braking":
        n = rng.choice((1, 2))
        return [{"decel_g": rng.uniform(0.5, 0.7), **({"brake": rng.uniform(0.85, 1.0)} if i == 0 else {})}
                for i in range(n)]
    if kind == "near_miss":
        first = {"decel_g": rng.uniform(0.45, 0.6), "brake": rng.uniform(0.8, 1.0)}
        swerve = sign * rng.uniform(0.45, 0.6)
        if rng.random() < 0.5:
            return [{**first, "ay": swerve}]
        return [first, {"ay": swerve}]
    if kind == "crash":
        return [{"crash": True, "ax_peak": -rng.uniform(4.0, 8.0), "ay": sign * rng.uniform(0.5, 2.0),
                 "az": 1.0 + rng.uniform(0.3, 1.5)}]
    if kind == "phone_distraction":
        n = rng.randint(12, 40)
        steps: list[dict[str, Any]] = [{} for _ in range(n)]
        steps[0]["phone"] = ["unlock", "screen_on"]
        steps[-1]["phone"] = ["screen_off"]
        return steps
    if kind == "pothole":
        return [{"az": 1.0 + rng.uniform(0.5, 2.3)}]  # some exceed the crash detector threshold
    if kind == "firm_brake":
        # Mean decel 0.35-0.375 g gives a peak (~1.15x mean + noise) of ~0.40-0.44 g: it trips the
        # 0.40 g DETECTOR but stays below the 0.45 g POLICY threshold, so "no_incident" is the
        # correct label under the policy agents are given. The old 0.33-0.43 range produced peaks
        # up to ~0.50 g that ARE hard braking by policy: a label bug found by eval error analysis (M-14).
        return [{"decel_g": rng.uniform(0.35, 0.375)}]
    raise ValueError(kind)


@dataclass
class _Second:
    t: int
    v: float
    lat: float
    lon: float
    heading: float
    limit: float
    ax_mean: float = 0.0
    ax_peak: float = 0.0
    ay_peak: float = 0.0
    az_peak: float = 1.0
    brake: float | None = None
    phone: list[str] = field(default_factory=list)


def _simulate_trip(
    rng: random.Random, cfg: SimConfig, driver: Driver, vehicle: Vehicle, start_ms: int
) -> tuple[list[Event], list[Label]]:
    p = PERSONALITIES[driver.personality]
    trip_id = _uuid(rng)
    drive_s = rng.randint(*cfg.trip_seconds)

    # Road segments with speed limits, and red-light stops at some boundaries.
    segments: list[tuple[int, int, float]] = []
    t0 = 0
    while t0 < drive_s:
        dur = rng.randint(60, 240)
        segments.append((t0, t0 + dur, rng.choice(LIMITS_MPS)))
        t0 += dur
    stops = {s: rng.randint(8, 30) for (s, _, _) in segments[1:] if rng.random() < 0.5}

    def limit_at(t: int) -> float:
        for s, e, lim in segments:
            if s <= t < e:
                return lim
        return segments[-1][2]

    # Schedule incidents and hard negatives.
    kinds: list[str] = []
    if rng.random() < cfg.incident_rate:
        for _ in range(1 + (rng.random() < 0.3)):
            kinds.append(rng.choices(INCIDENT_KINDS, weights=cfg.incident_weights)[0])
    if kinds.count("crash") > 1:
        kinds = [k for k in kinds if k != "crash"] + ["crash"]
    parked_phone = False
    if rng.random() < cfg.negative_rate:
        neg = rng.choice(NEGATIVE_KINDS)
        if neg == "parked_phone":
            parked_phone = True
        else:
            kinds.append(neg)
    slots = list(range(45, max(46, drive_s - 45), 40))
    times = sorted(rng.sample(slots, min(len(kinds), len(slots))))
    kinds = kinds[: len(times)]
    # A crash ends the trip, so it must be the last scheduled item.
    if "crash" in kinds:
        kinds.remove("crash")
        kinds.append("crash")
    pending = list(zip(times, kinds))

    stop_decel = rng.uniform(*p["stop_decel"])
    lat, lon = cfg.center[0] + rng.uniform(-0.05, 0.05), cfg.center[1] + rng.uniform(-0.05, 0.05)
    heading = rng.uniform(0, 360)
    v = 0.0
    seconds: list[_Second] = []
    active: list[dict[str, Any]] = []
    active_label: dict[str, Any] | None = None
    raw_labels: list[dict[str, Any]] = []
    stopping, hold = False, 0
    turn_left = 0
    turn_rate = 0.0
    crashed_at: int | None = None
    rest_after_crash = 0
    tail_left: int | None = None
    phone_tail_at: tuple[int, int] | None = None

    t = 0
    while True:
        lim = limit_at(t)
        rec = _Second(t=t, v=v, lat=lat, lon=lon, heading=heading, limit=lim)
        step: dict[str, Any] = {}

        if crashed_at is None and tail_left is None and t < drive_s:
            if not active and pending and t >= pending[0][0] and not stopping:
                kind = pending[0][1]
                if v >= _MIN_SPEED[kind]:
                    pending.pop(0)
                    active = _steps_for(kind, rng)
                    active_label = {"kind": kind, "t_start": t, "t_end": t + len(active) - 1}
                    raw_labels.append(active_label)
            if not active and t in stops and not stopping:
                stopping, hold = True, stops[t]
        if active:
            step = active.pop(0)

        # Longitudinal dynamics for this second.
        if crashed_at is not None:
            v_new = 0.0
        elif step.get("crash"):
            v_new = 0.0
            crashed_at = t
            rest_after_crash = rng.randint(30, 60)
        elif "decel_g" in step:
            v_new = max(0.0, v - step["decel_g"] * G)
        else:
            ending = t >= drive_s
            target = 0.0 if (stopping or ending) else lim * p["speed_factor"]
            a = max(-stop_decel, min(p["accel"], target - v))
            v_new = max(0.0, v + a)
            if stopping and v_new == 0.0 and v == 0.0:
                hold -= 1
                if hold <= 0:
                    stopping = False
                    if rng.random() < 0.5:  # turn when pulling away
                        turn_left, turn_rate = 6, rng.choice((-15.0, 15.0))

        # Heading / lateral acceleration.
        dheading = rng.gauss(0, 1.5)
        if turn_left > 0:
            dheading += turn_rate
            turn_left -= 1
        v_avg = (v + v_new) / 2
        ay = max(-0.3, min(0.3, v_avg * math.radians(dheading) / G)) + rng.gauss(0, 0.02)
        heading = (heading + dheading) % 360

        ax_mean = (v_new - v) / G
        if abs(ax_mean) > 0.05:
            ax_peak = ax_mean * 1.15 + rng.gauss(0, 0.015)
        else:
            ax_peak = ax_mean + rng.gauss(0, 0.02)
        rec.ax_mean = round(ax_mean, 4)
        rec.ax_peak = round(step.get("ax_peak", ax_peak), 4)
        rec.ay_peak = round(step.get("ay", ay), 4)
        rec.az_peak = round(step.get("az", 1.0 + rng.gauss(0, 0.03)), 4)
        if "brake" in step:
            rec.brake = round(step["brake"], 3)
        rec.phone = list(step.get("phone", []))
        seconds.append(rec)

        # Integrate position.
        dist = v_avg
        lat += dist * math.cos(math.radians(heading)) / 111_320
        lon += dist * math.sin(math.radians(heading)) / (111_320 * math.cos(math.radians(lat)))
        v = v_new
        t += 1

        # Termination logic.
        if crashed_at is not None:
            if t - crashed_at > rest_after_crash:
                break
            continue
        if t >= drive_s and v == 0.0 and not active:
            if tail_left is None:
                tail_left = rng.randint(30, 60) if parked_phone else 3
                if parked_phone:
                    on = t + 3
                    phone_tail_at = (on, on + rng.randint(15, min(25, tail_left - 5)))
            if phone_tail_at and t == phone_tail_at[0]:
                active = [{"phone": ["unlock", "screen_on"]}]
                raw_labels.append({"kind": "parked_phone", "t_start": t, "t_end": phone_tail_at[1]})
            elif phone_tail_at and t == phone_tail_at[1]:
                active = [{"phone": ["screen_off"]}]
            tail_left -= 1
            if tail_left <= 0 and not active:
                break
        if t > drive_s + 600:  # safety net; should never trigger
            raise RuntimeError("trip simulation failed to terminate")

    # ---- Build events (sorted by time, seq assigned afterwards) ----
    proto: list[tuple[int, int, str, dict[str, Any], tuple[str, int] | None]] = []
    # (time_ms, tiebreak, type, payload, label-key)
    proto.append((start_ms, 0, "trip_start", {"driver_id": driver.driver_id, "vehicle_id": vehicle.vehicle_id}, None))
    distance_m = 0.0
    for i, s in enumerate(seconds):
        base = start_ms + 500 + s.t * 1000
        spd = max(0.0, s.v + rng.gauss(0, 0.2)) if s.v > 0 else 0.0
        proto.append((base, 1, "gps", {
            "lat": round(s.lat + rng.gauss(0, 5) / 111_320, 6),
            "lon": round(s.lon + rng.gauss(0, 5) / 111_320, 6),
            "speed_mps": round(min(spd, 89.0), 2),
            "speed_limit_mps": s.limit,
            "heading_deg": round(s.heading % 360, 1) % 360,
        }, ("gps", s.t)))
        if s.brake is not None:
            proto.append((base + 100, 2, "brake", {"pressure": s.brake}, ("brake", s.t)))
        for j, action in enumerate(s.phone):
            proto.append((base + 200 + j * 10, 3, "phone", {"action": action}, ("phone", s.t)))
        proto.append((base + 500, 4, "accel", {
            "ax_mean": s.ax_mean, "ax_peak": s.ax_peak, "ay_peak": s.ay_peak, "az_peak": s.az_peak,
        }, ("accel", s.t)))
        nxt = seconds[i + 1].v if i + 1 < len(seconds) else 0.0
        distance_m += (s.v + nxt) / 2
    end_ms = start_ms + 500 + len(seconds) * 1000
    if rng.random() < cfg.note_rate:
        proto.append((end_ms - 50, 5, "driver_note", {"text": rng.choice(_NOTES)}, None))
    proto.sort(key=lambda x: (x[0], x[1]))

    events: list[Event] = []
    ids_by_key: dict[tuple[str, int], list[str]] = {}
    for seq, (tm, _, typ, payload, key) in enumerate(proto):
        ev = Event(event_id=_uuid(rng), trip_id=trip_id, seq=seq, event_time_ms=tm, type=typ, payload=payload)
        events.append(ev)
        if key:
            ids_by_key.setdefault(key, []).append(str(ev.event_id))
    last_seq = len(events)
    events.append(Event(event_id=_uuid(rng), trip_id=trip_id, seq=last_seq, event_time_ms=end_ms,
                        type="trip_end", payload={"distance_km": round(distance_m / 1000, 3), "last_seq": last_seq}))

    labels: list[Label] = []
    for lab in raw_labels:
        kind, ts, te = lab["kind"], lab["t_start"], lab["t_end"]
        ev_ids: list[str] = []
        for sec in range(ts, te + 1):
            if kind in ("phone_distraction", "parked_phone"):
                if ("phone", sec) in ids_by_key:
                    ev_ids += ids_by_key[("phone", sec)] + ids_by_key.get(("gps", sec), [])
            else:
                ev_ids += ids_by_key.get(("accel", sec), []) + ids_by_key.get(("brake", sec), [])
        if kind == "firm_brake":
            # Ground truth follows the classification POLICY applied to the generated signal:
            # if sensor noise pushed the measured peak past 0.45 g, this IS hard braking.
            peak = min(seconds[sec].ax_peak for sec in range(ts, te + 1))
            if peak <= -0.45:
                kind = "hard_braking"
        labels.append(Label(
            label_id=str(_uuid(rng)), trip_id=str(trip_id), kind=kind,
            is_incident=kind in INCIDENT_KINDS,
            t_start_ms=start_ms + 500 + ts * 1000, t_end_ms=start_ms + 500 + (te + 1) * 1000,
            evidence_event_ids=tuple(ev_ids),
        ))
    return events, labels


def simulate(cfg: SimConfig = SimConfig()) -> SimOutput:
    rng = random.Random(cfg.seed)
    drivers: list[Driver] = []
    vehicles: list[Vehicle] = []
    for i in range(cfg.n_drivers):
        d = Driver(
            driver_id=f"drv_{_hex8(rng)}",
            display_name=f"{rng.choice(_FIRST)} {rng.choice(_LAST)}",
            phone=f"+1-555-01{i % 100:02d}",
            personality=rng.choice(list(PERSONALITIES)),
        )
        drivers.append(d)
        vehicles.append(Vehicle(vehicle_id=f"veh_{_hex8(rng)}", driver_id=d.driver_id,
                                make_model=rng.choice(_VEHICLES)))
    events: list[Event] = []
    labels: list[Label] = []
    for _ in range(cfg.n_trips):
        idx = rng.randrange(len(drivers))
        start_ms = cfg.start_epoch_ms + rng.randint(0, 14 * 24 * 3600) * 1000
        ev, lab = _simulate_trip(rng, cfg, drivers[idx], vehicles[idx], start_ms)
        events += ev
        labels += lab
    return SimOutput(drivers=drivers, vehicles=vehicles, events=events, labels=labels)
