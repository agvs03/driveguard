"""Scoring for one (case, sample) and aggregation across a run.

The system is non-deterministic, so every headline number is computed over
k samples per case, with a 95% bootstrap CI. The bootstrap resamples CASES
(keeping all samples of a case together), because cases, not samples, are the
independent unit.

Definitions (also in the report footer):
  accuracy            predicted == expected. Escalations / "inconclusive" count as wrong.
  coverage            share of case-samples with a decision (not escalated / inconclusive)
  selective_accuracy  accuracy among decided case-samples
  macro_f1            mean F1 over the 5 classes; abstentions are misses for the expected class
  citation_precision  cited events inside the ground-truth window (label window -2 s .. +15 s,
                      or the detected window for unlabeled cases) / all cited events
  evidence_hit_rate   share of reports citing >= 1 of the injected label's evidence events
  hallucination_rate  share of FIRST-DRAFT findings with a fabricated/ungrounded citation or a number
                      that doesn't match the cited events (validator codes E_UNKNOWN, E_UNGROUNDED,
                      E_NUMBER, E_NO_SOURCE). Measures the model, before the gate fixes anything.
  shipped_hallucination_rate  same, on reports that reached a human as valid (should be 0: the gate)
  fabricated_id_rate  E_UNKNOWN citations / all first-draft citations
  stability           per case, share of samples agreeing with the case's modal prediction
"""

from __future__ import annotations

import random
import statistics
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

from driveguard.evals.dataset import CLASSES, EvalCase

HALLUCINATION_CODES = {"E_UNKNOWN", "E_UNGROUNDED", "E_NUMBER", "E_NO_SOURCE"}
WINDOW_BEFORE_MS, WINDOW_AFTER_MS = 2_000, 15_000


@dataclass
class CaseResult:
    case_key: str
    sample: int
    expected: str
    truth_source: str
    detected_kind: str
    predicted: str | None
    status: str
    reason: str
    correct: bool
    decided: bool
    cited: list[str] = field(default_factory=list)
    cited_in_window: int = 0
    evidence_hit: bool | None = None
    first_draft_findings: int = 0
    first_draft_hallucinated_findings: int = 0
    first_draft_citations: int = 0
    first_draft_fabricated_ids: int = 0
    repaired: bool = False
    shipped_valid: bool = False
    shipped_hallucinated_findings: int = 0
    shipped_findings: int = 0
    cost_usd: float = 0.0
    latency_s: float = 0.0
    model_calls: int = 0
    tool_calls: int = 0
    run_id: str = ""
    case_id: str | None = None
    bucket: str = ""


def _bad_findings(validation: dict[str, Any]) -> set[str]:
    return {e["where"] for e in validation.get("errors", []) if e["code"] in HALLUCINATION_CODES}


def score_case(case: EvalCase, sample: int, outcome: Any, spans: list[dict[str, Any]],
               event_times: dict[str, int], detected_window: tuple[int, int]) -> CaseResult:
    report = outcome.report or {}
    predicted = report.get("classification") if outcome.status == "awaiting_approval" else None
    if predicted == "inconclusive":
        predicted = None
    r = CaseResult(case_key=case.case_key, sample=sample, expected=case.expected, truth_source=case.truth_source,
                   detected_kind=case.detected_kind, predicted=predicted, status=outcome.status,
                   reason=outcome.reason, correct=predicted == case.expected, decided=predicted is not None,
                   cost_usd=outcome.cost_usd, latency_s=outcome.latency_s, model_calls=outcome.model_calls,
                   tool_calls=outcome.tool_calls, run_id=outcome.run_id, case_id=outcome.case_id)

    # First draft = the writer's first finished output + the first validation span.
    drafts = [s["output"]["output"] for s in spans if s["kind"] == "state" and s["actor"] == "report_writer"
              and s["output"].get("status") == "finished"]
    vals = [s["output"] for s in spans if s["kind"] == "validation" and s["actor"] == "citation_validator"]
    if drafts and vals:
        first, v0 = drafts[0], vals[0]
        n_find = len(first["findings"]) + len(first.get("contradicting_evidence", []))
        r.first_draft_findings = n_find
        r.first_draft_hallucinated_findings = len(_bad_findings(v0))
        r.first_draft_citations = v0.get("citations_checked", 0)
        r.first_draft_fabricated_ids = sum(1 for e in v0.get("errors", []) if e["code"] == "E_UNKNOWN")
        r.repaired = len(vals) > 1
        if outcome.status == "awaiting_approval":
            last_draft, last_v = drafts[-1], vals[-1]
            r.shipped_valid = bool(last_v.get("ok"))
            r.shipped_findings = len(last_draft["findings"]) + len(last_draft.get("contradicting_evidence", []))
            r.shipped_hallucinated_findings = len(_bad_findings(last_v))

    if report:
        r.cited = list(dict.fromkeys(e for f in report["findings"] + report.get("contradicting_evidence", [])
                                     for e in f["cited_event_ids"]))
        lo, hi = case.label_window if case.label_window else detected_window
        lo, hi = lo - WINDOW_BEFORE_MS, hi + WINDOW_AFTER_MS
        r.cited_in_window = sum(1 for e in r.cited if e in event_times and lo <= event_times[e] <= hi)
        if case.truth_source == "incident" and case.label_evidence_event_ids:
            r.evidence_hit = bool(set(r.cited) & set(case.label_evidence_event_ids))
    r.bucket = bucket(r)
    return r


def bucket(r: CaseResult) -> str:
    """Error-analysis bucket (empty for a fully correct, well-cited result)."""
    if r.status == "escalated":
        kind = r.reason.split(":", 1)[0] if r.reason else "unknown"
        return f"escalated:{kind}"
    if r.status in ("failed", "skipped", "skipped_budget"):
        return r.status
    if not r.decided:
        return "abstained"
    if not r.correct:
        return f"wrong:{r.expected}->{r.predicted}"
    if r.evidence_hit is False:
        return "correct_but_missed_evidence"
    if r.first_draft_hallucinated_findings:
        return "correct_after_repair"
    return ""


# --------------------------------------------------------------- aggregate --- #

def _f1(results: list[CaseResult]) -> float:
    f1s = []
    for k in CLASSES:
        tp = sum(r.predicted == k and r.expected == k for r in results)
        fp = sum(r.predicted == k and r.expected != k for r in results)
        fn = sum(r.expected == k and r.predicted != k for r in results)
        if tp + fp + fn == 0:
            continue
        p = tp / (tp + fp) if tp + fp else 0.0
        rc = tp / (tp + fn) if tp + fn else 0.0
        f1s.append(2 * p * rc / (p + rc) if p + rc else 0.0)
    return sum(f1s) / len(f1s) if f1s else 0.0


def _ratio(num: float, den: float) -> float | None:
    return num / den if den else None


def _pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    return s[min(len(s) - 1, max(0, round(q * (len(s) - 1))))]


METRICS: dict[str, Callable[[list[CaseResult]], float | None]] = {
    "accuracy": lambda rs: _ratio(sum(r.correct for r in rs), len(rs)),
    "coverage": lambda rs: _ratio(sum(r.decided for r in rs), len(rs)),
    "selective_accuracy": lambda rs: _ratio(sum(r.correct for r in rs if r.decided), sum(r.decided for r in rs)),
    "macro_f1": lambda rs: _f1(rs) if rs else None,
    "citation_precision": lambda rs: _ratio(sum(r.cited_in_window for r in rs), sum(len(r.cited) for r in rs)),
    "evidence_hit_rate": lambda rs: _ratio(sum(bool(r.evidence_hit) for r in rs if r.evidence_hit is not None),
                                           sum(r.evidence_hit is not None for r in rs)),
    "hallucination_rate": lambda rs: _ratio(sum(r.first_draft_hallucinated_findings for r in rs),
                                            sum(r.first_draft_findings for r in rs)),
    "shipped_hallucination_rate": lambda rs: _ratio(sum(r.shipped_hallucinated_findings for r in rs if r.shipped_valid),
                                                    sum(r.shipped_findings for r in rs if r.shipped_valid)),
    "fabricated_id_rate": lambda rs: _ratio(sum(r.first_draft_fabricated_ids for r in rs),
                                            sum(r.first_draft_citations for r in rs)),
    "repair_rate": lambda rs: _ratio(sum(r.repaired for r in rs), len(rs)),
    "escalation_rate": lambda rs: _ratio(sum(r.status == "escalated" for r in rs), len(rs)),
    "cost_per_case_usd": lambda rs: statistics.fmean(r.cost_usd for r in rs) if rs else None,
    "latency_p50_s": lambda rs: _pct([r.latency_s for r in rs], 0.5),
    "latency_p95_s": lambda rs: _pct([r.latency_s for r in rs], 0.95),
    "model_calls_per_case": lambda rs: statistics.fmean(r.model_calls for r in rs) if rs else None,
    "tool_calls_per_case": lambda rs: statistics.fmean(r.tool_calls for r in rs) if rs else None,
}
HIGHER_IS_BETTER = {"accuracy", "coverage", "selective_accuracy", "macro_f1", "citation_precision",
                    "evidence_hit_rate"}
LOWER_IS_BETTER = {"hallucination_rate", "shipped_hallucination_rate", "fabricated_id_rate", "repair_rate",
                   "escalation_rate", "cost_per_case_usd", "latency_p50_s", "latency_p95_s",
                   "model_calls_per_case", "tool_calls_per_case"}


def _by_case(results: list[CaseResult]) -> dict[str, list[CaseResult]]:
    g: dict[str, list[CaseResult]] = defaultdict(list)
    for r in results:
        g[r.case_key].append(r)
    return g


def bootstrap_ci(results: list[CaseResult], metric: str, reps: int = 1000, seed: int = 0) -> tuple[float, float] | None:
    g = _by_case(results)
    keys = sorted(g)
    if not keys:
        return None
    rng = random.Random(seed)
    vals = []
    for _ in range(reps):
        sample = [r for k in (rng.choice(keys) for _ in keys) for r in g[k]]
        v = METRICS[metric](sample)
        if v is not None:
            vals.append(v)
    if not vals:
        return None
    vals.sort()
    return vals[int(0.025 * (len(vals) - 1))], vals[int(0.975 * (len(vals) - 1))]


def stability(results: list[CaseResult]) -> float | None:
    per = []
    for rs in _by_case(results).values():
        if len(rs) < 2:
            continue
        preds = [r.predicted for r in rs]
        per.append(Counter(preds).most_common(1)[0][1] / len(preds))
    return statistics.fmean(per) if per else None


def aggregate(results: list[CaseResult], reps: int = 1000) -> dict[str, Any]:
    scored = [r for r in results if r.status != "skipped_budget"]
    out: dict[str, Any] = {"n_case_samples": len(scored), "n_cases": len(_by_case(scored)),
                           "skipped_budget": len(results) - len(scored), "metrics": {}}
    for name, fn in METRICS.items():
        out["metrics"][name] = {"value": fn(scored), "ci95": bootstrap_ci(scored, name, reps)}
    out["metrics"]["stability"] = {"value": stability(scored), "ci95": None}
    out["confusion"] = {e: dict(Counter(r.predicted or "abstain" for r in scored if r.expected == e))
                        for e in CLASSES}
    out["per_class"] = {}
    for k in CLASSES:
        sub = [r for r in scored if r.expected == k]
        out["per_class"][k] = {"n": len(sub), "recall": METRICS["accuracy"](sub),
                               "precision": _ratio(sum(r.correct for r in scored if r.predicted == k),
                                                   sum(r.predicted == k for r in scored))}
    out["buckets"] = dict(Counter(r.bucket for r in scored if r.bucket))
    out["by_truth_source"] = {s: METRICS["accuracy"]([r for r in scored if r.truth_source == s])
                              for s in ("incident", "hard_negative", "unlabeled")}
    return out


def compare(base: list[CaseResult], cand: list[CaseResult], reps: int = 1000, seed: int = 0) -> dict[str, Any]:
    """Paired bootstrap over the cases both runs share: the same resampled case set is
    scored under both runs, so case difficulty cancels out of the delta."""
    gb, gc = _by_case(base), _by_case(cand)
    keys = sorted(set(gb) & set(gc))
    rng = random.Random(seed)
    draws = [[rng.choice(keys) for _ in keys] for _ in range(reps)] if keys else []
    out: dict[str, Any] = {"shared_cases": len(keys), "metrics": {}}
    for name, fn in METRICS.items():
        b = fn([r for k in keys for r in gb[k]])
        c = fn([r for k in keys for r in gc[k]])
        deltas = []
        for d in draws:
            vb, vc = fn([r for k in d for r in gb[k]]), fn([r for k in d for r in gc[k]])
            if vb is not None and vc is not None:
                deltas.append(vc - vb)
        deltas.sort()
        ci = (deltas[int(0.025 * (len(deltas) - 1))], deltas[int(0.975 * (len(deltas) - 1))]) if deltas else None
        out["metrics"][name] = {"base": b, "cand": c, "delta": (c - b) if b is not None and c is not None else None,
                                "ci95": ci}
    changed = []
    for k in keys:
        pb = Counter(r.predicted for r in gb[k]).most_common(1)[0][0]
        pc = Counter(r.predicted for r in gc[k]).most_common(1)[0][0]
        if pb != pc:
            changed.append({"case_key": k, "expected": gb[k][0].expected, "base": pb, "cand": pc})
    out["changed_predictions"] = changed
    return out


# Regression tolerances for the gate (absolute for rates; relative for cost/latency).
TOLERANCE = {"accuracy": 0.02, "macro_f1": 0.02, "citation_precision": 0.02, "evidence_hit_rate": 0.05,
             "hallucination_rate": 0.02, "shipped_hallucination_rate": 0.0, "fabricated_id_rate": 0.01,
             "escalation_rate": 0.05}
RELATIVE_TOLERANCE = {"cost_per_case_usd": 0.20, "model_calls_per_case": 0.20}


# Safety metrics fail fast on the POINT estimate: with ~40 cases a real jump in hallucinations
# is often not "significant", and a false alarm is cheaper than shipping one (DECISIONS D-34).
SAFETY = {"hallucination_rate", "shipped_hallucination_rate", "fabricated_id_rate"}


def regressions(cmp: dict[str, Any]) -> list[str]:
    """Quality metrics regress when the whole 95% CI of the delta is past the tolerance (so noise
    doesn't fail builds); safety metrics regress when the point delta is past it.
    Deterministic runs give a zero-width CI, so any change beyond tolerance counts."""
    bad = []
    for name, m in cmp["metrics"].items():
        if m["ci95"] is None or m["base"] is None:
            continue
        lo, hi = m["ci95"]
        if name in SAFETY:
            if m["delta"] is not None and m["delta"] > TOLERANCE[name]:
                bad.append(f"{name}: {m['base']:.3f} -> {m['cand']:.3f} (safety metric: point delta "
                           f"{m['delta']:+.3f} > {TOLERANCE[name]})")
        elif name in TOLERANCE:
            tol = TOLERANCE[name]
            if name in HIGHER_IS_BETTER and hi < -tol:
                bad.append(f"{name}: {m['base']:.3f} -> {m['cand']:.3f} (delta CI [{lo:+.3f}, {hi:+.3f}])")
            if name in LOWER_IS_BETTER and lo > tol:
                bad.append(f"{name}: {m['base']:.3f} -> {m['cand']:.3f} (delta CI [{lo:+.3f}, {hi:+.3f}])")
        elif name in RELATIVE_TOLERANCE and m["base"]:
            if lo > RELATIVE_TOLERANCE[name] * m["base"]:
                bad.append(f"{name}: {m['base']:.4f} -> {m['cand']:.4f} (+{100 * m['delta'] / m['base']:.0f}%)")
    return bad


def result_to_row(r: CaseResult) -> dict[str, Any]:
    return asdict(r)
