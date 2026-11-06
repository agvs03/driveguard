"""Transparent, reproducible trip risk score.

score = sum_i weight_i * clip((raw_i - floor_i) / (cap_i - floor_i), 0, 1)

Weights sum to 100, so each factor's `contribution` is in score points and the
contributions sum exactly to the score (asserted in tests). Every factor lists
the event ids it was computed from, so a report can cite them. The same inputs
always give the same `score_id` (uuid5 of trip, weights version and input hash).
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from functools import lru_cache
from importlib import resources
from typing import Any

from pydantic import BaseModel

from driveguard.pipeline.detector import StoredEvent

_NS = uuid.UUID("0b7c4f3e-2a91-4d8e-b5c6-7f1a2e3d4c5b")
SPEEDING_FACTOR = 1.05
MOVING_MPS = 1.0
MAX_EVIDENCE = 20


class FactorValue(BaseModel):
    name: str
    raw_value: float
    unit: str
    description: str
    evidence_event_ids: list[str]
    evidence_truncated: bool = False


class ScoredFactor(FactorValue):
    weight: float
    floor: float
    cap: float
    normalized: float
    contribution: float


class RiskScore(BaseModel):
    score_id: str
    trip_id: str
    weights_version: str
    score: float
    factors: list[ScoredFactor]
    input_hash: str


@lru_cache(maxsize=4)
def load_weights(version: str = "weights-v1") -> dict[str, Any]:
    fname = {"weights-v1": "weights_v1.json"}.get(version)
    if fname is None:
        raise ValueError(f"unknown weights version {version!r}")
    w = json.loads(resources.files("driveguard.risk").joinpath(fname).read_text(encoding="utf-8"))
    total = sum(f["weight"] for f in w["factors"].values())
    if abs(total - 100) > 1e-9:
        raise ValueError(f"weights must sum to 100, got {total}")
    for name, f in w["factors"].items():
        if not f["cap"] > f["floor"]:
            raise ValueError(f"factor {name}: cap must exceed floor")
    return w


def _ev(ids: list[str]) -> tuple[list[str], bool]:
    return ids[:MAX_EVIDENCE], len(ids) > MAX_EVIDENCE


def compute_factors(
    events: list[StoredEvent], incidents: list[dict[str, Any]], distance_km: float | None,
    weights_version: str = "weights-v1",
) -> list[FactorValue]:
    meta = load_weights(weights_version)["factors"]
    active = [i for i in incidents if not i.get("superseded")]
    out: list[FactorValue] = []

    def add(name: str, raw: float, ids: list[str]) -> None:
        ev, trunc = _ev(ids)
        out.append(FactorValue(name=name, raw_value=round(raw, 4), unit=meta[name]["unit"],
                               description=meta[name]["description"], evidence_event_ids=ev,
                               evidence_truncated=trunc))

    braking = [i for i in active if i["kind"] in ("hard_braking", "near_miss")]
    rate = (len(braking) / distance_km * 100) if distance_km and distance_km > 0 else 0.0
    add("hard_brake_rate", rate, [e for i in braking for e in i["trigger_event_ids"]])

    crash_ids = {e for i in active if i["kind"] == "crash" for e in i["trigger_event_ids"]}
    accel = [e for e in events if e.type == "accel" and e.event_id not in crash_ids]
    worst = min(accel, key=lambda e: e.payload["ax_peak"], default=None)
    add("max_decel_g", max(0.0, -worst.payload["ax_peak"]) if worst else 0.0, [worst.event_id] if worst else [])

    gps = [e for e in events if e.type == "gps"]
    moving = [e for e in gps if e.payload["speed_mps"] > MOVING_MPS]
    speeding = [e for e in moving if e.payload["speed_mps"] > e.payload["speed_limit_mps"] * SPEEDING_FACTOR]
    add("speeding_share", len(speeding) / len(moving) if moving else 0.0, [e.event_id for e in speeding])

    phone = [i for i in active if i["kind"] == "phone_distraction"]
    add("phone_moving_s", float(sum(i["details"].get("moving_seconds", 0) for i in phone)),
        [e for i in phone for e in i["trigger_event_ids"]])

    def is_night(ms: int) -> bool:
        h = datetime.fromtimestamp(ms / 1000, tz=timezone.utc).hour
        return h >= 22 or h < 5

    add("night_share", sum(1 for e in gps if is_night(e.event_time_ms)) / len(gps) if gps else 0.0, [])
    add("crash", 1.0 if crash_ids else 0.0, sorted(crash_ids))
    return out


def score(trip_id: str, factors: list[FactorValue], weights_version: str = "weights-v1") -> RiskScore:
    w = load_weights(weights_version)
    scored: list[ScoredFactor] = []
    for f in factors:
        m = w["factors"][f.name]
        norm = min(1.0, max(0.0, (f.raw_value - m["floor"]) / (m["cap"] - m["floor"])))
        scored.append(ScoredFactor(**f.model_dump(), weight=m["weight"], floor=m["floor"], cap=m["cap"],
                                   normalized=round(norm, 6), contribution=round(m["weight"] * norm, 6)))
    if {s.name for s in scored} != set(w["factors"]):
        raise ValueError("factor set does not match weights file")
    total = round(sum(s.contribution for s in scored), 6)
    input_hash = hashlib.sha256(json.dumps(
        [f.model_dump() for f in factors], sort_keys=True).encode()).hexdigest()
    sid = uuid.uuid5(_NS, f"{trip_id}:{w['version']}:{input_hash}")
    return RiskScore(score_id=str(sid), trip_id=trip_id, weights_version=w["version"], score=total,
                     factors=scored, input_hash=input_hash)


def explain(rs: RiskScore) -> list[str]:
    """Deterministic, human-readable one-liner per factor, largest contribution first."""
    lines = []
    for f in sorted(rs.factors, key=lambda f: -f.contribution):
        lines.append(
            f"{f.name}: raw {f.raw_value} {f.unit} -> normalised {f.normalized:.2f} x weight {f.weight:g} "
            f"= {f.contribution:.2f} points ({len(f.evidence_event_ids)} evidence events"
            f"{', truncated' if f.evidence_truncated else ''})"
        )
    return lines
