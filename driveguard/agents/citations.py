"""Deterministic citation validator: the gate before human approval.

For every finding (and contradicting-evidence item) in a report:
  E_UNKNOWN      cited id is not an event of this case's trip (or does not exist)
  E_UNGROUNDED   cited event's full record never appeared in a tool result in
                 this run's trace (the model could not have read it)
  E_NUMBER       a number with a unit (g, m/s, km/h) in the claim does not
                 match any value in the cited events (within tolerance)
  E_NO_SOURCE    the claim states such a number but cites no event of a type
                 that could contain it (e.g. "0.6 g" citing only GPS events)
Warnings (do not block): W_SECONDS (durations are not checked),
W_CLASSIFICATION (report disagrees with triage), W_IRRELEVANT (nothing cited
inside the incident window).
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any

_NUM = re.compile(r"(-?\d+(?:\.\d+)?)\s*(g\b|m/s\b|km/h\b|s\b|seconds\b)", re.IGNORECASE)
# Classification-policy thresholds may be quoted in prose ("above the 0.45 g policy
# threshold"). They are exempt ONLY when threshold wording is adjacent, so "the
# deceleration was 0.45 g" is still checked against the cited events.
def _policy_constants() -> set[tuple[str, float]]:
    """Every number-with-unit in the POLICY text the agents are given. Derived, not hand-listed:
    the hand-written list missed '0 m/s' and caused a false positive in the first real-model eval."""
    from driveguard.agents.specialists import POLICY
    out = {(m.group(2).lower(), abs(float(m.group(1)))) for m in _NUM.finditer(POLICY)}
    # "between 0.40 and 0.45 g": the unit is written once, after the second number.
    for m in re.finditer(r"between (\d+(?:\.\d+)?) and \d+(?:\.\d+)?\s*(g|m/s)\b", POLICY):
        out.add((m.group(2), float(m.group(1))))
    return out | {("g", 3.0)}  # the detector's crash trigger, which agents also discuss


_THRESHOLD_WORDS = re.compile(
    r"threshold|policy|above|below|at least|>=|<=|under|over|exceed|approach|rules? out|pattern|criteri|"
    r"required|needed|limit|cutoff|cut-off|definition|would need|never|not\b|no\b|instead|rather than|remain",
    re.IGNORECASE)


def _is_quoted_threshold(text: str, m: re.Match[str], value: float, unit: str) -> bool:
    if (unit, value) not in _policy_constants():
        return False
    # Sentence-level context: "the crash criterion, which requires the vehicle to stay at 0 m/s" put the
    # threshold word 40+ chars away (real Claude red-team run, M-24). A fixed character window kept missing.
    start = max(text.rfind(". ", 0, m.start()), text.rfind("; ", 0, m.start()), -1) + 1
    end_candidates = [i for i in (text.find(". ", m.end()), text.find("; ", m.end())) if i != -1]
    end = min(end_candidates) if end_candidates else len(text)
    return bool(_THRESHOLD_WORDS.search(text[start:end]))
G_TOL_ABS, G_TOL_REL = 0.02, 0.05
MPS_TOL = 0.3
KMH_TOL = 1.0


@dataclass
class Issue:
    code: str
    where: str
    detail: str


@dataclass
class ValidationResult:
    ok: bool
    errors: list[Issue] = field(default_factory=list)
    warnings: list[Issue] = field(default_factory=list)
    citations_checked: int = 0
    numbers_checked: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "errors": [i.__dict__ for i in self.errors],
                "warnings": [i.__dict__ for i in self.warnings], "citations_checked": self.citations_checked,
                "numbers_checked": self.numbers_checked}

    def feedback(self) -> str:
        return "\n".join(f"- {i.where}: {i.code} {i.detail}" for i in self.errors)


def _g_values(payload: dict[str, Any]) -> list[float]:
    keys = ("ax_peak", "ax_mean", "ay_peak", "az_peak")
    vals = [abs(float(payload[k])) for k in keys if k in payload]
    if all(k in payload for k in ("ax_peak", "ay_peak", "az_peak")):
        vals.append(math.sqrt(payload["ax_peak"] ** 2 + payload["ay_peak"] ** 2 + payload["az_peak"] ** 2))
    return vals


def _speeds(payload: dict[str, Any]) -> list[float]:
    return [float(payload[k]) for k in ("speed_mps", "speed_limit_mps") if k in payload]


def validate_report(report: dict[str, Any], *, trip_event_ids: set[str], seen: dict[str, tuple[dict[str, Any], str]],
                    incident: dict[str, Any], triage_classification: str | None) -> ValidationResult:
    res = ValidationResult(ok=True)
    items = [("findings", i, f) for i, f in enumerate(report.get("findings", []))] + \
            [("contradicting_evidence", i, f) for i, f in enumerate(report.get("contradicting_evidence", []))]
    window = (incident["t_start_ms"] - 5_000, incident["t_end_ms"] + 20_000)
    any_relevant = False

    for section, idx, f in items:
        where = f"{section}[{idx}]"
        cited = list(dict.fromkeys(f.get("cited_event_ids", [])))
        events = []
        for eid in cited:
            res.citations_checked += 1
            if eid not in trip_event_ids:
                res.errors.append(Issue("E_UNKNOWN", where, f"{eid} is not an event of this case's trip"))
                continue
            if eid not in seen:
                res.errors.append(Issue("E_UNGROUNDED", where, f"{eid} was never retrieved by a tool in this run"))
                continue
            ev = seen[eid][0]
            events.append(ev)
            if window[0] <= ev.get("event_time_ms", 0) <= window[1]:
                any_relevant = True

        for m in _NUM.finditer(f.get("claim", "")):
            value, unit = abs(float(m.group(1))), m.group(2).lower()
            if unit in ("s", "seconds"):
                res.warnings.append(Issue("W_SECONDS", where, f"duration {m.group(0)!r} not verified"))
                continue
            if _is_quoted_threshold(f.get("claim", ""), m, value, unit):
                continue
            res.numbers_checked += 1
            if unit == "g":
                pool = [v for e in events if e.get("type") == "accel" for v in _g_values(e["payload"])]
                ok = any(abs(v - value) <= max(G_TOL_ABS, G_TOL_REL * v) for v in pool)
            elif unit == "m/s":
                pool = [v for e in events if e.get("type") == "gps" for v in _speeds(e["payload"])]
                ok = any(abs(v - value) <= MPS_TOL for v in pool)
            else:  # km/h
                pool = [v * 3.6 for e in events if e.get("type") == "gps" for v in _speeds(e["payload"])]
                ok = any(abs(v - value) <= KMH_TOL for v in pool)
            if not pool:
                res.errors.append(Issue("E_NO_SOURCE", where, f"{m.group(0)!r} cites no event that could contain it"))
            elif not ok:
                res.errors.append(Issue("E_NUMBER", where, f"{m.group(0)!r} matches no value in the cited events "
                                                           f"(closest {min(pool, key=lambda v: abs(v - value)):.3f})"))

    # The summary has no citations of its own: every number in it must match a value
    # in some event cited anywhere in the report (closes the "uncited number in prose" hole).
    all_cited = [seen[e][0] for _, _, f in items for e in f.get("cited_event_ids", []) if e in seen]
    for m in _NUM.finditer(report.get("summary", "")):
        value, unit = abs(float(m.group(1))), m.group(2).lower()
        if unit in ("s", "seconds") or _is_quoted_threshold(report.get("summary", ""), m, value, unit):
            continue
        res.numbers_checked += 1
        if unit == "g":
            pool = [v for e in all_cited if e.get("type") == "accel" for v in _g_values(e["payload"])]
            ok = any(abs(v - value) <= max(G_TOL_ABS, G_TOL_REL * v) for v in pool)
        else:
            mult = 3.6 if unit == "km/h" else 1.0
            tol = KMH_TOL if unit == "km/h" else MPS_TOL
            pool = [v * mult for e in all_cited if e.get("type") == "gps" for v in _speeds(e["payload"])]
            ok = any(abs(v - value) <= tol for v in pool)
        if not ok:
            res.errors.append(Issue("E_SUMMARY_NUMBER", "summary",
                                    f"{m.group(0)!r} does not match any event cited in the findings"))

    if triage_classification and report.get("classification") != triage_classification:
        res.warnings.append(Issue("W_CLASSIFICATION", "classification",
                                  f"report says {report.get('classification')}, triage said {triage_classification}"))
    if items and not any_relevant:
        res.warnings.append(Issue("W_IRRELEVANT", "findings", "no cited event falls inside the incident window"))
    res.ok = not res.errors
    return res
