"""Detector quality against simulator ground truth (precursor to Phase 4 evals).

Matching rule: a detected incident matches a label when they share a trip, the
time ranges overlap (with `slack_ms` tolerance) and, for incident labels, the
kinds agree. Detections overlapping a hard-negative label count as false
positives attributed to that negative's kind; detections matching nothing are
"unlabeled" false positives.
"""

from __future__ import annotations

from collections import Counter
from typing import Any, Iterable

from driveguard.sim.generator import INCIDENT_KINDS, Label


def _overlaps(a0: int, a1: int, b0: int, b1: int, slack: int) -> bool:
    return a0 <= b1 + slack and b0 <= a1 + slack


def evaluate_detector(labels: Iterable[Label], incidents: list[dict[str, Any]], slack_ms: int = 2000) -> dict[str, Any]:
    labels = list(labels)
    used: set[str] = set()
    per_kind: dict[str, Counter[str]] = {k: Counter() for k in INCIDENT_KINDS}
    fp_sources: Counter[str] = Counter()

    for lab in (l for l in labels if l.is_incident):
        match = next(
            (i for i in incidents
             if i["incident_id"] not in used and i["trip_id"] == lab.trip_id and i["kind"] == lab.kind
             and _overlaps(i["t_start_ms"], i["t_end_ms"], lab.t_start_ms, lab.t_end_ms, slack_ms)),
            None,
        )
        if match:
            used.add(match["incident_id"])
            per_kind[lab.kind]["tp"] += 1
        else:
            per_kind[lab.kind]["fn"] += 1

    for inc in incidents:
        if inc["incident_id"] in used:
            continue
        per_kind.setdefault(inc["kind"], Counter())["fp"] += 1
        src = next(
            (l.kind for l in labels if l.trip_id == inc["trip_id"]
             and _overlaps(inc["t_start_ms"], inc["t_end_ms"], l.t_start_ms, l.t_end_ms, slack_ms)),
            "unlabeled",
        )
        fp_sources[f"{inc['kind']}<-{src}"] += 1

    out: dict[str, Any] = {"per_kind": {}, "false_positive_sources": dict(fp_sources)}
    for kind, c in per_kind.items():
        tp, fp, fn = c["tp"], c["fp"], c["fn"]
        out["per_kind"][kind] = {
            "tp": tp, "fp": fp, "fn": fn,
            "precision": round(tp / (tp + fp), 3) if tp + fp else None,
            "recall": round(tp / (tp + fn), 3) if tp + fn else None,
        }
    return out
