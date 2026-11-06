import { useEffect, useState } from "react";
import { api } from "../api";
import type { CaseSummary } from "../types";
import { fmtTime, shortId } from "../steps";
import { ErrorBox, StatusPill } from "./common";

const FILTERS = ["", "AWAITING_APPROVAL", "ESCALATED", "INVESTIGATING", "CLOSED"];

export function CaseList({ selected, onSelect }: { selected: string | null; onSelect: (id: string) => void }) {
  const [status, setStatus] = useState("");
  const [cases, setCases] = useState<CaseSummary[] | null>(null);
  const [error, setError] = useState<unknown>(null);

  useEffect(() => {
    let live = true;
    setCases(null);
    api.cases(status || undefined)
      .then((r) => live && setCases(r.cases))
      .catch((e) => live && setError(e));
    return () => { live = false; };
  }, [status]);

  return (
    <nav className="case-list" aria-label="Cases">
      <div className="case-list-head">
        <h2>Cases</h2>
        <select value={status} onChange={(e) => setStatus(e.target.value)} aria-label="Filter by status">
          {FILTERS.map((f) => <option key={f} value={f}>{f ? f.replace(/_/g, " ").toLowerCase() : "all statuses"}</option>)}
        </select>
      </div>
      {error ? <ErrorBox error={error} /> : null}
      {cases === null && !error && <div className="muted pad">Loading…</div>}
      {cases?.length === 0 && <div className="muted pad">No cases.</div>}
      <ul>
        {cases?.map((c) => (
          <li key={c.case_id}>
            <button className={`case-row ${c.case_id === selected ? "active" : ""}`} onClick={() => onSelect(c.case_id)}>
              <span className="row1"><code>{shortId(c.case_id)}</code><StatusPill status={c.status} /></span>
              <span className="row2">{c.classification?.replace(/_/g, " ") ?? "unclassified"}
                {c.confidence != null && <span className="muted"> · {Math.round(c.confidence * 100)}%</span>}</span>
              <span className="row3 muted">{fmtTime(c.updated_at_ms)}</span>
            </button>
          </li>
        ))}
      </ul>
    </nav>
  );
}
