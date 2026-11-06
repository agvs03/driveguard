import { useEffect, useMemo, useState } from "react";
import { api, ApiError } from "../api";
import { buildPhases, fmtTime, shortId } from "../steps";
import type { CaseDetail, Me, TraceResponse } from "../types";
import { Empty, ErrorBox, JsonView, StatusPill } from "./common";
import { Evidence } from "./Evidence";
import { Report } from "./Report";
import { Timeline } from "./Timeline";

export function CaseView({ caseId, runId, me, onSelectRun }: {
  caseId: string; runId: string | null; me: Me; onSelectRun: (runId: string) => void;
}) {
  const [c, setCase] = useState<CaseDetail | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [trace, setTrace] = useState<TraceResponse | null>(null);
  const [traceError, setTraceError] = useState<unknown>(null);
  const [citation, setCitation] = useState<string | null>(null);
  const [highlight, setHighlight] = useState<number | null>(null);
  const canTrace = me.scopes.includes("trace:read");

  useEffect(() => {
    let live = true;
    setCase(null);
    setError(null);
    setCitation(null);
    api.case(caseId).then((x) => live && setCase(x)).catch((e) => live && setError(e));
    return () => { live = false; };
  }, [caseId]);

  const activeRun = runId ?? c?.report?.run_id ?? c?.runs[0]?.run_id ?? null;

  useEffect(() => {
    let live = true;
    setTrace(null);
    setTraceError(null);
    setHighlight(null);
    if (!activeRun || !canTrace) return;
    api.trace(activeRun).then((t) => live && setTrace(t)).catch((e) => live && setTraceError(e));
    return () => { live = false; };
  }, [activeRun, canTrace]);

  const phases = useMemo(() => (trace ? buildPhases(trace.spans) : []), [trace]);

  if (error) return <div className="pad"><ErrorBox error={error} /></div>;
  if (!c) return <div className="pad muted">Loading case…</div>;

  const run = c.runs.find((r) => r.run_id === activeRun);
  const openCitation = (id: string) => {
    setCitation(id);
    const seq = trace?.citations[id]?.seq;
    setHighlight(seq ?? null);
  };
  // A report from an older run is not explained by this run's trace: say so instead of mismatching citations.
  const reportMatchesRun = !c.report || c.report.run_id === activeRun;

  return (
    <div className="case-view">
      <header className="case-head">
        <div>
          <h1>Case <code>{shortId(c.case_id)}</code> <StatusPill status={c.status} /></h1>
          <p className="muted">
            Detected <strong>{c.incident.kind.replace(/_/g, " ")}</strong> on trip <code>{shortId(c.trip_id)}</code>
            {c.trip && <> · driver <code>{shortId(c.trip.driver_id)}</code></>} · window {fmtTime(c.incident.t_start_ms)}
            {c.incident.superseded && <span className="check bad"> superseded by re-detection</span>}
          </p>
        </div>
        {run && (
          <dl className="run-stats">
            <div><dt>Run</dt><dd>
              {c.runs.length > 1 ? (
                <select value={run.run_id} onChange={(e) => onSelectRun(e.target.value)} aria-label="Select run">
                  {c.runs.map((r) => <option key={r.run_id} value={r.run_id}>{shortId(r.run_id)} · {r.status}</option>)}
                </select>
              ) : <code>{shortId(run.run_id)}</code>}
            </dd></div>
            <div><dt>Model</dt><dd>{run.llm}</dd></div>
            <div><dt>Cost</dt><dd>${run.cost_usd.toFixed(3)}</dd></div>
            <div><dt>Time</dt><dd>{run.outcome.latency_s?.toFixed(1) ?? "-"} s</dd></div>
            <div><dt>Calls</dt><dd>{run.model_calls} model · {run.tool_calls} tool</dd></div>
            {trace && (
              <div><dt>Audit chain</dt><dd className={trace.integrity.ok ? "good-text" : "bad-text"}>
                {trace.integrity.ok ? `verified, ${trace.integrity.spans} spans`
                  : `BROKEN at span #${trace.integrity.first_bad_seq}`}
              </dd></div>
            )}
          </dl>
        )}
      </header>
      {run?.outcome.reason && <div className="error-box">Escalation reason: {run.outcome.reason}</div>}

      <div className="case-grid">
        <div className="col">
          {!reportMatchesRun && (
            <div className="note-box">This report was written by run <code>{shortId(c.report!.run_id)}</code>; the
              timeline shows run <code>{shortId(activeRun)}</code>.</div>
          )}
          <Report c={c} trace={reportMatchesRun ? trace : null} onOpenCitation={openCitation} />
          <section className="card">
            <h2>Case history</h2>
            <ol className="history">
              {c.history.map((h, i) => (
                <li key={i}>
                  <span className="muted small">{fmtTime(h.at_ms)}</span>{" "}
                  <strong>{h.action}</strong> {h.from_status && <>{h.from_status} → {h.to_status}</>}{" "}
                  <span className="muted">by {h.actor_sub} ({h.actor_role})</span>
                  {Object.keys(h.detail).length > 0 && <details><summary className="small">detail</summary><JsonView value={h.detail} /></details>}
                </li>
              ))}
            </ol>
            <h3>Notes</h3>
            {c.notes.length === 0 && <p className="muted">No notes.</p>}
            <ul className="notes">
              {c.notes.map((n) => (
                <li key={n.note_id} className={n.text.startsWith("[guardrail]") ? "note-guard" : ""}>
                  <span className="muted small">{fmtTime(n.created_at_ms)} · {n.author_sub}</span>
                  <p>{n.text}</p>
                </li>
              ))}
            </ul>
          </section>
        </div>
        <div className="col">
          <section className="card">
            <h2>Agent reasoning</h2>
            <p className="muted small">Every model call, tool call and check from the hash-chained audit trace, in order.
              Click a step for its inputs and outputs.</p>
            {!canTrace && <Empty>Your token lacks <code>trace:read</code>, so the reasoning steps are hidden.</Empty>}
            {traceError ? (
              traceError instanceof ApiError && traceError.status === 403
                ? <Empty>Not allowed to read this run's trace.</Empty> : <ErrorBox error={traceError} />
            ) : null}
            {canTrace && !trace && !traceError && activeRun && <p className="muted">Loading trace…</p>}
            {!activeRun && <Empty>No investigation run yet.</Empty>}
            {trace && <Timeline phases={phases} highlightSeq={highlight} />}
          </section>
        </div>
      </div>
      {citation && (
        <Evidence eventId={citation} trace={reportMatchesRun ? trace : null} onClose={() => setCitation(null)}
          onJump={(seq) => setHighlight(seq)} />
      )}
    </div>
  );
}
