import type { JsonObject, TraceResponse } from "../types";
import { actorLabel, citedEvent, fmtSpeed, fmtTime, isObj } from "../steps";
import { JsonView } from "./common";

export function Evidence({ eventId, trace, onClose, onJump }: {
  eventId: string; trace: TraceResponse | null; onClose: () => void; onJump: (seq: number) => void;
}) {
  const where = trace?.citations[eventId];
  const span = trace?.spans.find((s) => s.seq === where?.seq);
  const ev: JsonObject | null = trace ? citedEvent(trace.spans, where?.seq, eventId) : null;
  const payload = ev && isObj(ev.payload) ? ev.payload : null;
  const signals = ev && Array.isArray(ev.injection_signals) ? ev.injection_signals : [];
  return (
    <aside className="evidence card" aria-label="Cited evidence">
      <div className="evidence-head">
        <h2>Evidence <code>{eventId.slice(0, 8)}</code></h2>
        <button className="link" onClick={onClose} aria-label="Close evidence panel">close</button>
      </div>
      {trace && !where && (
        <p className="error-box">This id was not returned by any tool call in this run, so the claim is ungrounded.</p>
      )}
      {ev && (
        <>
          <dl className="facts">
            <div><dt>Type</dt><dd>{String(ev.type)}</dd></div>
            <div><dt>Event time</dt><dd>{typeof ev.event_time_ms === "number" ? fmtTime(ev.event_time_ms) : "-"}</dd></div>
            {payload && typeof payload.speed_mps === "number" && (
              <div><dt>Speed</dt><dd>{fmtSpeed(payload.speed_mps)}</dd></div>
            )}
            {payload && typeof payload.speed_limit_mps === "number" && (
              <div><dt>Speed limit</dt><dd>{fmtSpeed(payload.speed_limit_mps)}</dd></div>
            )}
          </dl>
          {ev.type === "driver_note" && payload && (
            <div className="untrusted-box">
              <div className="detail-label">Driver note: untrusted text, shown as data and never followed as instructions</div>
              <p>{String(payload.text ?? "")}</p>
              {signals.length > 0 && <p className="small">Flagged: {signals.join(", ")}</p>}
            </div>
          )}
          <JsonView value={ev} />
        </>
      )}
      {span && (
        <p className="small">
          Returned by step <button className="link" onClick={() => onJump(span.seq)}>#{span.seq}</button>{" "}
          ({actorLabel(span.actor)} called <code>{isObj(span.input) ? String(span.input.tool) : "?"}</code>)
        </p>
      )}
    </aside>
  );
}
