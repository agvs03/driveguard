"""Rule-based incident detector.

A pure function of a trip's stored events. Because it never depends on
arrival order or on previous runs, re-running it after late events arrive is
idempotent; that property is what makes late-event handling simple.

It is deliberately imperfect (thresholds sit inside the range of some hard
negatives) so that later phases have real false positives for agents to reject.
"""

from __future__ import annotations

import bisect
import json
import math
import uuid
from dataclasses import dataclass, field
from typing import Any

DETECTOR_VERSION = "rules-v1"
_NS = uuid.UUID("6f1d8a52-3c0e-4f6b-9a51-0d1e2f3a4b5c")

HARD_BRAKE_G = 0.40
LATERAL_G = 0.35
CRASH_RESULTANT_G = 3.0
NEAR_MISS_WINDOW_MS = 2000
GROUP_GAP_MS = 2000
PHONE_MIN_MOVING_S = 10
MOVING_MPS = 5.0
PHONE_START_MPS = 2.0


@dataclass(frozen=True)
class StoredEvent:
    event_id: str
    seq: int
    event_time_ms: int
    type: str
    payload: dict[str, Any]


@dataclass
class Incident:
    incident_id: str
    trip_id: str
    kind: str
    t_start_ms: int
    t_end_ms: int
    trigger_event_ids: list[str]
    details: dict[str, Any] = field(default_factory=dict)
    detector_version: str = DETECTOR_VERSION


def _incident(trip_id: str, kind: str, evs: list[StoredEvent], details: dict[str, Any]) -> Incident:
    t0 = min(e.event_time_ms for e in evs)
    t1 = max(e.event_time_ms for e in evs)
    iid = uuid.uuid5(_NS, f"{trip_id}:{kind}:{t0}:{DETECTOR_VERSION}")
    return Incident(
        incident_id=str(iid), trip_id=trip_id, kind=kind, t_start_ms=t0, t_end_ms=t1,
        trigger_event_ids=[e.event_id for e in sorted(evs, key=lambda e: (e.event_time_ms, e.seq))],
        details=details,
    )


def _group(evs: list[StoredEvent]) -> list[list[StoredEvent]]:
    groups: list[list[StoredEvent]] = []
    for e in sorted(evs, key=lambda e: e.event_time_ms):
        if groups and e.event_time_ms - groups[-1][-1].event_time_ms <= GROUP_GAP_MS:
            groups[-1].append(e)
        else:
            groups.append([e])
    return groups


def detect(trip_id: str, events: list[StoredEvent]) -> list[Incident]:
    accel = [e for e in events if e.type == "accel"]
    gps = sorted((e for e in events if e.type == "gps"), key=lambda e: e.event_time_ms)
    gps_t = [e.event_time_ms for e in gps]

    def speed_at(t: int) -> float:
        i = bisect.bisect_right(gps_t, t) - 1
        return gps[i].payload["speed_mps"] if i >= 0 else 0.0

    def resultant(e: StoredEvent) -> float:
        p = e.payload
        return math.sqrt(p["ax_peak"] ** 2 + p["ay_peak"] ** 2 + p["az_peak"] ** 2)

    incidents: list[Incident] = []

    crash_w = [e for e in accel if resultant(e) >= CRASH_RESULTANT_G]
    crash_times = [e.event_time_ms for e in crash_w]
    for g in _group(crash_w):
        incidents.append(_incident(trip_id, "crash", g, {"peak_resultant_g": round(max(resultant(e) for e in g), 3)}))

    def near_crash(t: int) -> bool:
        return any(abs(t - c) <= NEAR_MISS_WINDOW_MS for c in crash_times)

    brake_w = [e for e in accel if e.payload["ax_peak"] <= -HARD_BRAKE_G and not near_crash(e.event_time_ms)]
    lateral_w = [e for e in accel if abs(e.payload["ay_peak"]) >= LATERAL_G and not near_crash(e.event_time_ms)]
    for g in _group(brake_w):
        t0, t1 = g[0].event_time_ms, g[-1].event_time_ms
        swerves = [e for e in lateral_w if t0 - NEAR_MISS_WINDOW_MS <= e.event_time_ms <= t1 + NEAR_MISS_WINDOW_MS]
        details = {"max_decel_g": round(-min(e.payload["ax_peak"] for e in g), 3)}
        if swerves:
            details["max_lateral_g"] = round(max(abs(e.payload["ay_peak"]) for e in swerves), 3)
            uniq = {e.event_id: e for e in g + swerves}
            incidents.append(_incident(trip_id, "near_miss", list(uniq.values()), details))
        else:
            incidents.append(_incident(trip_id, "hard_braking", g, details))

    # Phone sessions: screen on (or unlock) .. screen off (or end of trip).
    phone = sorted((e for e in events if e.type == "phone"), key=lambda e: (e.event_time_ms, e.seq))
    session: list[StoredEvent] = []
    sessions: list[list[StoredEvent]] = []
    for e in phone:
        action = e.payload["action"]
        if action in ("unlock", "screen_on"):
            session.append(e)
        elif action == "screen_off" and session:
            session.append(e)
            sessions.append(session)
            session = []
    if session:
        sessions.append(session)
    trip_end_t = max((e.event_time_ms for e in events), default=0)
    for s in sessions:
        start = s[0].event_time_ms
        end = s[-1].event_time_ms if s[-1].payload["action"] == "screen_off" else trip_end_t
        moving = sum(1 for g in gps if start <= g.event_time_ms <= end and g.payload["speed_mps"] > MOVING_MPS)
        if speed_at(start) > PHONE_START_MPS and moving >= PHONE_MIN_MOVING_S:
            incidents.append(_incident(trip_id, "phone_distraction", s, {
                "moving_seconds": moving, "speed_at_start_mps": round(speed_at(start), 2),
            }))

    incidents.sort(key=lambda i: (i.t_start_ms, i.kind))
    return incidents


def stored_event_from_row(row: Any) -> StoredEvent:
    return StoredEvent(row["event_id"], row["seq"], row["event_time_ms"], row["type"], json.loads(row["payload_json"]))
