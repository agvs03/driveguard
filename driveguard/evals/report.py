"""Markdown reports: single run (with error analysis) and before/after comparison."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from driveguard.evals.dataset import CLASSES
from driveguard.evals.metrics import CaseResult, HIGHER_IS_BETTER, aggregate, compare, regressions

_ORDER = ["accuracy", "macro_f1", "coverage", "selective_accuracy", "citation_precision", "evidence_hit_rate",
          "hallucination_rate", "shipped_hallucination_rate", "fabricated_id_rate", "repair_rate", "escalation_rate",
          "stability", "cost_per_case_usd", "latency_p50_s", "latency_p95_s", "model_calls_per_case",
          "tool_calls_per_case"]


def _f(v: Any, name: str = "") -> str:
    if v is None:
        return "n/a"
    if name.startswith("cost"):
        return f"${v:.4f}"
    if name.startswith("latency"):
        return f"{v:.2f}s"
    if name.endswith("per_case"):
        return f"{v:.1f}"
    return f"{v:.3f}"


def run_report(meta: sqlite3.Row, results: list[CaseResult]) -> str:
    agg = aggregate(results)
    m = agg["metrics"]
    lines = [f"# Eval run `{meta['run_id']}` ({meta['label']})", "",
             f"- **Dataset:** {meta['dataset']} (hash `{meta['dataset_hash']}`), {agg['n_cases']} cases x "
             f"{meta['samples']} samples = {agg['n_case_samples']} case-samples"
             + (f", **{agg['skipped_budget']} skipped by the run budget**" if agg["skipped_budget"] else ""),
             f"- **LLM:** {meta['llm']}  **Code:** `{meta['code_fingerprint']}`  **Git:** {meta['git_sha'] or 'n/a'}",
             f"- **Total cost:** ${meta['total_cost_usd'] or 0:.4f}", "",
             "## Metrics (95% bootstrap CI over cases)", "", "| metric | value | 95% CI |", "|---|---|---|"]
    for name in _ORDER:
        v = m[name]
        ci = v["ci95"]
        lines.append(f"| {name} | {_f(v['value'], name)} | "
                     f"{'n/a' if not ci else f'{_f(ci[0], name)} .. {_f(ci[1], name)}'} |")
    lines += ["", "## Confusion matrix (rows = expected, columns = predicted)", "",
              "| expected \\ predicted | " + " | ".join(CLASSES) + " | abstain |",
              "|---" * (len(CLASSES) + 2) + "|"]
    for e in CLASSES:
        row = agg["confusion"][e]
        lines.append(f"| **{e}** | " + " | ".join(str(row.get(p, 0)) for p in list(CLASSES) + ["abstain"]) + " |")
    lines += ["", "## Per class", "", "| class | n | recall | precision |", "|---|---|---|---|"]
    for k, v in agg["per_class"].items():
        lines.append(f"| {k} | {v['n']} | {_f(v['recall'])} | {_f(v['precision'])} |")
    lines += ["", f"Accuracy by truth source: " + ", ".join(f"{k}={_f(v)}" for k, v in agg["by_truth_source"].items()),
              "", "## Error analysis", ""]
    if not agg["buckets"]:
        lines.append("No failures.")
    else:
        lines += ["| bucket | count |", "|---|---|"] + [f"| {b} | {n} |" for b, n in
                                                         sorted(agg["buckets"].items(), key=lambda x: -x[1])]
        lines += ["", "### Failing case-samples", "",
                  "| case | sample | expected | detected | predicted | bucket | inspect |", "|---|---|---|---|---|---|---|"]
        for r in sorted((r for r in results if r.bucket), key=lambda r: (r.bucket, r.case_key, r.sample)):
            lines.append(f"| {r.case_key} | {r.sample} | {r.expected} | {r.detected_kind} | {r.predicted or '-'} | "
                         f"{r.bucket} | `trace --db data/evals/runs/{meta['run_id']}/sample-{r.sample}.db "
                         f"--case {r.case_id}` |")
    lines += ["", "<sub>Metric definitions: driveguard/evals/metrics.py module docstring.</sub>"]
    return "\n".join(lines) + "\n"


def compare_report(base_meta: sqlite3.Row, cand_meta: sqlite3.Row, base: list[CaseResult],
                   cand: list[CaseResult]) -> tuple[str, list[str]]:
    cmp = compare(base, cand)
    bad = regressions(cmp)
    lines = [f"# Before / after: `{base_meta['label']}` -> `{cand_meta['label']}`", "",
             f"- Base `{base_meta['run_id']}` ({base_meta['llm']}, code `{base_meta['code_fingerprint']}`)",
             f"- Candidate `{cand_meta['run_id']}` ({cand_meta['llm']}, code `{cand_meta['code_fingerprint']}`)",
             f"- {cmp['shared_cases']} shared cases; paired bootstrap 95% CI on the delta", "",
             "| metric | before | after | delta | 95% CI | |", "|---|---|---|---|---|---|"]
    for name in _ORDER:
        if name not in cmp["metrics"]:
            continue
        v = cmp["metrics"][name]
        d = v["delta"]
        ci = v["ci95"]
        arrow = ""
        if d not in (None, 0):
            good = (d > 0) == (name in HIGHER_IS_BETTER)
            arrow = "better" if good else "worse"
        lines.append(f"| {name} | {_f(v['base'], name)} | {_f(v['cand'], name)} | "
                     f"{'n/a' if d is None else f'{d:+.4f}'} | "
                     f"{'n/a' if not ci else f'{ci[0]:+.3f} .. {ci[1]:+.3f}'} | {arrow} |")
    lines += ["", "## Regressions (gate)", ""] + ([f"- {b}" for b in bad] or ["None."])
    lines += ["", "## Cases whose (modal) prediction changed", ""]
    if cmp["changed_predictions"]:
        lines += ["| case | expected | before | after |", "|---|---|---|---|"] + [
            f"| {c['case_key']} | {c['expected']} | {c['base']} | {c['cand']} |" for c in cmp["changed_predictions"]]
    else:
        lines.append("None.")
    return "\n".join(lines) + "\n", bad


def run_meta(conn: sqlite3.Connection, run_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM eval_runs WHERE run_id = ?", (run_id,)).fetchone()
    if row is None:
        raise SystemExit(f"no eval run {run_id}")
    return row


def summary_json(conn: sqlite3.Connection, run_id: str) -> dict[str, Any]:
    return json.loads(run_meta(conn, run_id)["summary_json"] or "{}")
