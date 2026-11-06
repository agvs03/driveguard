import { useEffect, useRef, useState } from "react";
import type { Phase, Step } from "../steps";
import { JsonView } from "./common";

const KIND_LABEL: Record<string, string> = {
  model_call: "model", tool_call: "tool", validation: "check", guard: "guard", run_start: "start",
  run_end: "end", human_decision: "human", state: "state",
};

function fmtLatency(ms: number): string {
  return ms >= 1000 ? `${(ms / 1000).toFixed(1)} s` : `${Math.round(ms)} ms`;
}

function StepRow({ step, highlighted }: { step: Step; highlighted: boolean }) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLLIElement>(null);
  useEffect(() => {
    if (highlighted) {
      setOpen(true);
      ref.current?.scrollIntoView?.({ behavior: "smooth", block: "center" }); // absent in jsdom
    }
  }, [highlighted]);
  return (
    <li ref={ref} id={`step-${step.seq}`} className={`step ${step.status} ${highlighted ? "highlight" : ""}`}>
      <button className="step-head" onClick={() => setOpen(!open)} aria-expanded={open}>
        <span className="seq">#{step.seq}</span>
        <span className={`kind kind-${step.kind}`}>{KIND_LABEL[step.kind] ?? step.kind}</span>
        <span className="step-title">
          {step.title}
          {step.untrusted && <span className="untrusted-tag">untrusted text</span>}
        </span>
        <span className="step-sub muted">{step.subtitle}</span>
        <span className="step-meta muted">
          {step.latencyMs > 0 && fmtLatency(step.latencyMs)}
          {step.costUsd > 0 && ` · $${step.costUsd.toFixed(4)}`}
        </span>
      </button>
      {open && (
        <div className="step-body">
          {step.details.length === 0 && <p className="muted small">No details.</p>}
          {step.details.map((d, i) => (
            <div key={i} className={`detail tone-${d.tone ?? "normal"}`}>
              <div className="detail-label">{d.label}</div>
              {d.text !== undefined ? <p className="detail-text">{d.text}</p> : <JsonView value={d.json ?? null} />}
            </div>
          ))}
        </div>
      )}
    </li>
  );
}

export function Timeline({ phases, highlightSeq }: { phases: Phase[]; highlightSeq: number | null }) {
  return (
    <div className="timeline">
      {phases.map((p) => (
        <section key={p.key} className={`phase phase-${p.status}`}>
          <h3>
            {p.title}
            <span className="muted small"> · {p.steps.length} step{p.steps.length === 1 ? "" : "s"}</span>
          </h3>
          {p.input !== undefined && p.input !== null && (
            <details className="phase-io">
              <summary>Task given to the agent</summary>
              <JsonView value={p.input} />
            </details>
          )}
          <ol className="steps">
            {p.steps.map((s) => <StepRow key={s.seq} step={s} highlighted={s.seq === highlightSeq} />)}
          </ol>
          {p.output !== undefined && p.output !== null && (
            <details className="phase-io" open={p.actor === "triage"}>
              <summary>Agent's final output</summary>
              <JsonView value={p.output} />
            </details>
          )}
        </section>
      ))}
    </div>
  );
}
