"""Re-score an eval run's first drafts with the CURRENT citation validator, offline.

Possible only because the audit trace keeps every draft and every tool result:
validator changes can be measured on real-model outputs without paying for new
model calls. Used to separate genuine model hallucinations from validator false
positives (DECISIONS M-17).
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from driveguard.agents.citations import validate_report
from driveguard.agents.trace import events_seen, load_spans
from driveguard.evals.metrics import HALLUCINATION_CODES


def revalidate_run(runs_dir: str | Path, run_id: str) -> dict[str, Any]:
    rows = []
    for db in sorted((Path(runs_dir) / run_id).glob("sample-*.db")):
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        for r in conn.execute("SELECT run_id, case_id, incident_id FROM investigation_runs "
                              "WHERE status IN ('awaiting_approval', 'escalated') ORDER BY started_at_ms"):
            spans = load_spans(conn, r["run_id"])
            drafts = [s["output"]["output"] for s in spans if s["kind"] == "state" and s["actor"] == "report_writer"
                      and s["output"].get("status") == "finished"]
            old = [s["output"] for s in spans if s["kind"] == "validation" and s["actor"] == "citation_validator"]
            triage = next((s["output"]["output"] for s in spans if s["kind"] == "state" and s["actor"] == "triage"
                           and s["output"].get("status") == "finished"), None)
            if not drafts or not old:
                continue
            inc = conn.execute("SELECT trip_id, t_start_ms, t_end_ms FROM detected_incidents WHERE incident_id = ?",
                               (r["incident_id"],)).fetchone()
            trip_events = {x[0] for x in conn.execute("SELECT event_id FROM raw_events WHERE trip_id = ?",
                                                      (inc["trip_id"],))}
            new = validate_report(drafts[0], trip_event_ids=trip_events, seen=events_seen(spans),
                                  incident=dict(inc), triage_classification=(triage or {}).get("classification"))
            old_bad = [e for e in old[0]["errors"] if e["code"] in HALLUCINATION_CODES]
            new_bad = [e.__dict__ for e in new.errors if e.code in HALLUCINATION_CODES]
            rows.append({"sample_db": db.name, "case_id": r["case_id"],
                         "findings": len(drafts[0]["findings"]) + len(drafts[0].get("contradicting_evidence", [])),
                         "old_flagged": len({e["where"] for e in old_bad}),
                         "new_flagged": len({e["where"] for e in new_bad}),
                         "cleared": [e for e in old_bad if e["where"] not in {n["where"] for n in new_bad}],
                         "still_flagged": new_bad})
        conn.close()
    total = sum(r["findings"] for r in rows)
    return {
        "run_id": run_id, "reports": len(rows), "first_draft_findings": total,
        "hallucination_rate_old_validator": (sum(r["old_flagged"] for r in rows) / total) if total else None,
        "hallucination_rate_new_validator": (sum(r["new_flagged"] for r in rows) / total) if total else None,
        "rows": rows,
    }


def main(runs_dir: str, run_id: str) -> None:
    out = revalidate_run(runs_dir, run_id)
    print(json.dumps({k: v for k, v in out.items() if k != "rows"}, indent=1))
    for r in out["rows"]:
        for e in r["cleared"]:
            print(f"cleared  {r['case_id'][:8]} {e['code']} {e['where']}: {e['detail'][:110]}")
        for e in r["still_flagged"]:
            print(f"FLAGGED  {r['case_id'][:8]} {e['code']} {e['where']}: {e['detail'][:110]}")
