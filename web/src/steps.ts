// Turns a run's audit-trace spans into what a reviewer reads: phases (one per agent, plus the
// orchestrator's own work) made of steps (model calls, tool calls, checks, guardrails).
// Pure functions, unit-tested in steps.test.ts.

import type { Json, JsonObject, TraceSpan } from "./types";

export type Tone = "normal" | "error" | "untrusted" | "ok";

export interface Detail {
  label: string;
  text?: string;
  json?: Json;
  tone?: Tone;
}

export interface Step {
  seq: number;
  kind: TraceSpan["kind"];
  actor: string;
  status: "ok" | "error";
  title: string;
  subtitle: string;
  latencyMs: number;
  costUsd: number;
  details: Detail[];
  untrusted: boolean; // the step carried attacker-controllable text (driver notes)
}

export interface Phase {
  key: string;
  actor: string;
  title: string;
  steps: Step[];
  input?: Json;
  output?: Json;
  status: "ok" | "error";
}

const AGENT_TITLES: Record<string, string> = {
  triage: "Triage agent",
  evidence: "Evidence agent",
  report_writer: "Report writer",
};

export function actorLabel(actor: string): string {
  if (AGENT_TITLES[actor]) return AGENT_TITLES[actor];
  if (actor === "citation_validator") return "Citation validator";
  if (actor === "output_guard") return "Output guard";
  if (actor.startsWith("human:")) return `Reviewer ${actor.slice(6)}`;
  if (actor.startsWith("service:")) return "Orchestrator (service)";
  return actor.charAt(0).toUpperCase() + actor.slice(1);
}

export function isObj(x: Json | undefined): x is JsonObject {
  return typeof x === "object" && x !== null && !Array.isArray(x);
}

function str(x: Json | undefined): string {
  return typeof x === "string" ? x : x === undefined || x === null ? "" : JSON.stringify(x);
}

function num(x: Json | undefined): number | undefined {
  return typeof x === "number" ? x : undefined;
}

export function fmtTokens(n: number | undefined): string {
  if (n === undefined) return "?";
  return n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(n);
}

export function totalInput(u: TraceSpan["usage"]): number | undefined {
  if (!u || u.input_tokens === undefined) return undefined;
  return u.input_tokens + (u.cache_read_input_tokens ?? 0) + (u.cache_creation_input_tokens ?? 0);
}

/** Every full event record (event_id + type + payload) inside a JSON value. */
export function eventsIn(x: Json | undefined, out: JsonObject[] = []): JsonObject[] {
  if (Array.isArray(x)) x.forEach((v) => eventsIn(v, out));
  else if (isObj(x)) {
    if (typeof x.event_id === "string" && typeof x.type === "string" && "payload" in x) out.push(x);
    Object.values(x).forEach((v) => eventsIn(v, out));
  }
  return out;
}

function hasUntrusted(x: Json | undefined): boolean {
  return eventsIn(x).some((e) => e.type === "driver_note");
}

function toolResultSummary(out: Json): string {
  if (isObj(out) && typeof out.code === "string" && typeof out.message === "string") return out.code;
  const evs = eventsIn(out);
  if (evs.length) return `${evs.length} event${evs.length === 1 ? "" : "s"}`;
  if (isObj(out)) {
    if (isObj(out.case)) return `case ${str(out.case.status)}`;
    if (typeof out.score === "number") return `risk score ${out.score}`;
    const keys = Object.keys(out);
    return keys.length ? keys.slice(0, 3).join(", ") : "empty";
  }
  if (Array.isArray(out)) return `${out.length} items`;
  return "ok";
}

export function spanToStep(s: TraceSpan): Step {
  const input = s.input;
  const output = s.output;
  const base: Step = {
    seq: s.seq,
    kind: s.kind,
    actor: s.actor,
    status: s.status,
    title: s.kind,
    subtitle: "",
    latencyMs: s.latency_ms,
    costUsd: s.cost_usd,
    details: [],
    untrusted: false,
  };
  switch (s.kind) {
    case "run_start": {
      const i = isObj(input) ? input : {};
      return { ...base, title: "Investigation started", subtitle: `${str(i.llm)} · ${str(i.model)}`,
        details: [{ label: "Limits", json: i.limits ?? null }] };
    }
    case "model_call": {
      const o = isObj(output) ? output : {};
      const u = s.usage ?? {};
      const blocks = Array.isArray(o.content) ? o.content : [];
      const details: Detail[] = [];
      for (const b of blocks) {
        if (!isObj(b)) continue;
        if (b.type === "text") details.push({ label: "Model output", text: str(b.text) });
        else if (b.type === "tool_use") details.push({ label: `Requested ${str(b.name)}`, json: b.input ?? null });
        else if (b.type === "thinking")
          details.push({ label: "Thinking", text: str(b.thinking) || "(thinking happened; its text is not returned by the API)" });
      }
      const i = isObj(input) ? input : {};
      if (Array.isArray(i.new_messages) && i.new_messages.length)
        details.push({ label: "New context sent to the model", json: i.new_messages, tone: hasUntrusted(i.new_messages) ? "untrusted" : "normal" });
      const tools = blocks.filter((b) => isObj(b) && b.type === "tool_use").length;
      return {
        ...base,
        title: tools ? `Model call → ${tools} tool request${tools === 1 ? "" : "s"}` : "Model call → answer",
        // Total input = uncached + cache reads + cache writes; "4 in" alone hid that most came from the cache.
        subtitle: `${fmtTokens(totalInput(u))} in` +
          (u.cache_read_input_tokens ? ` (${fmtTokens(u.cache_read_input_tokens)} cached)` : "") +
          ` · ${fmtTokens(u.output_tokens)} out` + (s.model ? ` · ${s.model}` : ""),
        details,
      };
    }
    case "tool_call": {
      const i = isObj(input) ? input : {};
      const untrusted = hasUntrusted(output);
      return {
        ...base,
        title: `Tool ${str(i.tool)}`,
        subtitle: s.status === "error" ? `error: ${toolResultSummary(output)}` : toolResultSummary(output),
        untrusted,
        details: [
          { label: "Arguments", json: i.arguments ?? null },
          { label: untrusted ? "Result (contains untrusted driver text)" : "Result", json: output,
            tone: s.status === "error" ? "error" : untrusted ? "untrusted" : "normal" },
        ],
      };
    }
    case "validation": {
      const o = isObj(output) ? output : {};
      const i = isObj(input) ? input : {};
      if (s.actor === "citation_validator") {
        const errs = Array.isArray(o.errors) ? o.errors.length : 0;
        const warns = Array.isArray(o.warnings) ? o.warnings.length : 0;
        return { ...base, title: `Citation check, attempt ${(num(i.attempt) ?? 0) + 1}`,
          subtitle: o.ok ? `passed${warns ? ` · ${warns} warning${warns === 1 ? "" : "s"}` : ""}` : `${errs} error${errs === 1 ? "" : "s"}`,
          details: [{ label: "Result", json: o, tone: o.ok ? "ok" : "error" }] };
      }
      return { ...base, title: "Output schema check failed", subtitle: str(o.errors).slice(0, 120),
        details: [{ label: "Errors", text: str(o.errors), tone: "error" }, { label: "Rejected output", text: str(i.text) }] };
    }
    case "guard": {
      const i = isObj(input) ? input : {};
      const o = isObj(output) ? output : {};
      let title = "Guardrail";
      if (s.actor === "output_guard") title = o.ok ? "Output guard passed" : "Output guard blocked the report";
      else if (i.action === "escalate") title = "Escalated to a human";
      else if (i.guardrail === "untrusted_text") title = "Instruction-like text found in driver notes";
      else if (i.guardrail) title = `Guardrail: ${str(i.guardrail)}`;
      else if (typeof i.tool === "string") title = `Blocked tool call ${i.tool}`;
      else if ("estimated_input_tokens" in i) title = "Token estimate below actual";
      const subtitle = str(o.reason ?? o.message ?? (Array.isArray(o.signals) ? o.signals.join(", ") : ""));
      return { ...base, title, subtitle: subtitle.slice(0, 160),
        details: [{ label: "Input", json: input }, { label: "Result", json: output, tone: s.status === "error" ? "error" : "ok" }] };
    }
    case "human_decision": {
      const i = isObj(input) ? input : {};
      return { ...base, title: `Reviewer decision: ${str(i.decision)}`, subtitle: str(i.comment).slice(0, 160),
        details: [{ label: "Comment", text: str(i.comment) }] };
    }
    case "run_end": {
      const o = isObj(output) ? output : {};
      return { ...base, title: `Investigation ended: ${str(o.status)}`,
        subtitle: `$${(num(o.cost_usd) ?? 0).toFixed(3)} · ${num(o.model_calls) ?? 0} model calls · ${num(o.tool_calls) ?? 0} tool calls · ${(num(o.latency_s) ?? 0).toFixed(1)} s`,
        details: o.reason ? [{ label: "Reason", text: str(o.reason), tone: "error" }] : [] };
    }
    case "state": {
      const o = isObj(output) ? output : {};
      return { ...base, title: `${actorLabel(s.actor)} ${str(o.status)}`, details: [] };
    }
  }
  return base;
}

/** Group spans into phases: each agent's started..finished span range is one phase; the
 * orchestrator's work in between (case writes, validation, guards) forms its own phases. */
export function buildPhases(spans: TraceSpan[]): Phase[] {
  const phases: Phase[] = [];
  let open: Phase | null = null; // the agent phase currently running
  let system: Phase | null = null;

  const systemPhase = (actor: string): Phase => {
    const isHuman = actor.startsWith("human:");
    const title = isHuman ? "Human review" : "Orchestrator";
    if (!system || system.title !== title) {
      system = { key: `sys-${phases.length}`, actor: isHuman ? actor : "orchestrator", title, steps: [], status: "ok" };
      phases.push(system);
    }
    return system;
  };

  for (const s of spans) {
    const out = isObj(s.output) ? s.output : {};
    if (s.kind === "state" && out.status === "started") {
      open = { key: `agent-${s.seq}`, actor: s.actor, title: actorLabel(s.actor), steps: [], status: "ok",
        input: isObj(s.input) ? (s.input.input ?? null) : null };
      phases.push(open);
      system = null;
      continue;
    }
    if (s.kind === "state" && out.status === "finished" && open && open.actor === s.actor) {
      open.output = out.output ?? null;
      open = null;
      continue;
    }
    const target: Phase = open && s.actor === open.actor ? open : systemPhase(s.actor);
    const step = spanToStep(s);
    target.steps.push(step);
    if (step.status === "error") target.status = "error";
  }
  return phases;
}

/** The full event record a citation points to, taken from the tool result that returned it. */
export function citedEvent(spans: TraceSpan[], seq: number | null | undefined, eventId: string): JsonObject | null {
  const span = spans.find((s) => s.seq === seq);
  if (!span) return null;
  return eventsIn(span.output).find((e) => e.event_id === eventId) ?? null;
}

// Same constant as the citation validator (driveguard/agents/citations.py): 1 m/s = 3600 / 1609.344 mph.
export const MPH_PER_MPS = 2.2369362920544;

/** "28.6 mph (12.8 m/s)": speeds are stored in m/s (as the sensors report them) and shown in mph. */
export function fmtSpeed(mps: number): string {
  return `${(mps * MPH_PER_MPS).toFixed(1)} mph (${mps} m/s)`;
}

export function shortId(id: string | null | undefined): string {
  return id ? id.slice(0, 8) : "-";
}

export function fmtTime(ms: number | null | undefined): string {
  if (!ms) return "-";
  return new Date(ms).toISOString().replace("T", " ").slice(0, 19) + "Z";
}
