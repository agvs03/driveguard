"""Report rendering and citation reconstruction from the audit trace."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from driveguard.agents.trace import events_seen, load_spans, verify_run


def render_markdown(r: dict[str, Any], validation: dict[str, Any]) -> str:
    ids: dict[str, str] = {}

    def ref(eid: str) -> str:
        ids.setdefault(eid, f"E{len(ids) + 1}")
        return f"[{ids[eid]}]"

    lines = [f"# Investigation report {r['report_id'][:8]}",
             "",
             f"- **Case:** `{r['case_id']}`  **Incident:** `{r['incident_id']}`  **Trip:** `{r['trip_id']}`",
             f"- **Classification:** {r['classification']} (confidence {r['confidence']:.2f})",
             f"- **Recommendation:** {r['recommendation']}",
             f"- **Trip risk score:** {r['risk_score']:.1f}/100" if r.get("risk_score") is not None
             else "- **Trip risk score:** n/a",
             f"- **Citation check:** {'passed' if validation['ok'] else 'FAILED'} "
             f"({validation['citations_checked']} citations, {validation['numbers_checked']} numbers checked)",
             "", "## Summary", "", r["summary"], "", "## Findings", ""]
    for f in r["findings"]:
        lines.append(f"- {f['claim']} {' '.join(ref(e) for e in f['cited_event_ids'])} "
                     f"_(confidence {f['confidence']:.2f})_")
    if r.get("contradicting_evidence"):
        lines += ["", "## Contradicting evidence", ""]
        for f in r["contradicting_evidence"]:
            lines.append(f"- {f['claim']} {' '.join(ref(e) for e in f['cited_event_ids'])}")
    if r.get("limitations"):
        lines += ["", "## Limitations", ""] + [f"- {x}" for x in r["limitations"]]
    lines += ["", "## Citations", ""] + [f"- {tag}: `{eid}`" for eid, tag in ids.items()]
    if not validation["ok"]:
        lines += ["", "## Validation errors", ""] + [f"- {e['where']}: {e['code']} {e['detail']}"
                                                     for e in validation["errors"]]
    return "\n".join(lines) + "\n"


def reconstruct(conn: sqlite3.Connection, report_id: str) -> dict[str, Any]:
    """For every finding: each cited event, the full record a tool returned, and the
    trace span (tool + arguments) that returned it. Also verifies the hash chain."""
    row = conn.execute("SELECT run_id, report_json FROM reports WHERE report_id = ?", (report_id,)).fetchone()
    if row is None:
        raise KeyError(report_id)
    report = json.loads(row[1])
    spans = load_spans(conn, row[0])
    by_span = {s["span_id"]: s for s in spans}
    seen = events_seen(spans)
    ok, bad = verify_run(conn, row[0])
    findings = []
    for f in report["findings"] + report["contradicting_evidence"]:
        cites = []
        for eid in f["cited_event_ids"]:
            if eid in seen:
                ev, sid = seen[eid]
                s = by_span[sid]
                cites.append({"event_id": eid, "event": ev, "span_seq": s["seq"], "actor": s["actor"],
                              "tool": s["input"]["tool"], "arguments": s["input"]["arguments"]})
            else:
                cites.append({"event_id": eid, "event": None, "span_seq": None})
        findings.append({"claim": f["claim"], "citations": cites})
    return {"report_id": report_id, "run_id": row[0], "trace_valid": ok, "first_bad_seq": bad,
            "findings": findings}
