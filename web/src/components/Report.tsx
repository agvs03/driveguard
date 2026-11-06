import type { CaseDetail, Finding, TraceResponse } from "../types";
import { shortId } from "../steps";

function Citation({ id, trace, onOpen }: { id: string; trace: TraceResponse | null; onOpen: (id: string) => void }) {
  const known = trace ? id in trace.citations : true;
  return (
    <button className={`cite ${known ? "" : "cite-missing"}`} onClick={() => onOpen(id)}
      aria-label={`cited event ${shortId(id)}${known ? "" : " (not returned by any tool call)"}`}
      title={known ? "Show the event and the tool call that returned it" : "Not returned by any tool call in this run"}>
      {shortId(id)}
    </button>
  );
}

function Findings({ title, items, trace, onOpen }: {
  title: string; items: Finding[]; trace: TraceResponse | null; onOpen: (id: string) => void;
}) {
  if (!items.length) return null;
  return (
    <section>
      <h3>{title}</h3>
      <ol className="findings">
        {items.map((f, i) => (
          <li key={i}>
            <p>{f.claim}</p>
            <div className="cites">
              <span className="muted small">cites</span>
              {f.cited_event_ids.map((e) => <Citation key={e} id={e} trace={trace} onOpen={onOpen} />)}
              <span className="muted small">· confidence {Math.round(f.confidence * 100)}%</span>
            </div>
          </li>
        ))}
      </ol>
    </section>
  );
}

export function Report({ c, trace, onOpenCitation }: {
  c: CaseDetail; trace: TraceResponse | null; onOpenCitation: (id: string) => void;
}) {
  const r = c.report;
  if (!r) return <section className="card"><h2>Report</h2><p className="muted">No report yet.</p></section>;
  const b = r.body;
  const v = r.validation;
  const guard = v.guardrails;
  const errors = v.errors ?? [];
  const warnings = v.warnings ?? [];
  return (
    <section className="card report">
      <div className="report-head">
        <h2>Report</h2>
        <div className="checks">
          <span className={`check ${errors.length ? "bad" : "good"}`}>
            {errors.length ? `${errors.length} citation error(s)` : "citations verified"}
          </span>
          {guard && (
            <span className={`check ${guard.ok ? "good" : "bad"}`}>
              {guard.ok ? "output guard passed" : `output guard: ${guard.violations.map((x) => x.code).join(", ")}`}
            </span>
          )}
          {!r.valid && <span className="check bad">not valid: escalated</span>}
        </div>
      </div>
      <dl className="facts">
        <div>
          <dt>Classification</dt>
          <dd>{b.classification.replace(/_/g, " ")} <span className="muted">({Math.round(b.confidence * 100)}%)</span></dd>
        </div>
        <div><dt>Recommendation</dt><dd>{b.recommendation.replace(/_/g, " ")}</dd></div>
        <div><dt>Risk score</dt><dd>{b.risk_score ?? "-"}</dd></div>
      </dl>
      <p className="summary">{b.summary}</p>
      <Findings title="Findings" items={b.findings} trace={trace} onOpen={onOpenCitation} />
      <Findings title="Contradicting evidence" items={b.contradicting_evidence} trace={trace} onOpen={onOpenCitation} />
      {b.limitations && b.limitations.length > 0 && (
        <section><h3>Limitations</h3><ul>{b.limitations.map((l, i) => <li key={i}>{l}</li>)}</ul></section>
      )}
      {warnings.length > 0 && (
        <details className="warnings">
          <summary>{warnings.length} validator warning(s)</summary>
          <ul>{warnings.map((w, i) => <li key={i}><code>{w.code}</code> {w.where} {w.detail}</li>)}</ul>
        </details>
      )}
    </section>
  );
}
