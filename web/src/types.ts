// Shapes returned by the DriveGuard API (driveguard/api/app.py). Span input/output are free-form JSON.

export type Json = null | boolean | number | string | Json[] | { [k: string]: Json };
export type JsonObject = { [k: string]: Json };

export interface Me {
  sub: string;
  role: string;
  scopes: string[];
  case_id: string | null;
  expires_at: number;
}

export interface CaseSummary {
  case_id: string;
  incident_id: string;
  trip_id: string;
  status: string;
  classification: string | null;
  confidence: number | null;
  version: number;
  updated_at_ms: number;
}

export interface Finding {
  claim: string;
  cited_event_ids: string[];
  confidence: number;
}

export interface ReportBody {
  report_id: string;
  classification: string;
  confidence: number;
  summary: string;
  findings: Finding[];
  contradicting_evidence: Finding[];
  recommendation: string;
  limitations?: string[];
  risk_score: number | null;
  risk_score_id: string | null;
  run_id: string;
}

export interface Validation {
  ok: boolean;
  errors?: { code: string; where: string; detail?: string }[];
  warnings?: { code: string; where: string; detail?: string }[];
  guardrails?: { ok: boolean; violations: { code: string; detail: string }[]; disclosures?: JsonObject[] };
  [k: string]: Json | undefined;
}

export interface RunSummary {
  run_id: string;
  status: string;
  llm: string;
  started_at_ms: number;
  ended_at_ms: number | null;
  cost_usd: number;
  model_calls: number;
  tool_calls: number;
  outcome: { reason?: string; report_id?: string | null; latency_s?: number };
}

export interface CaseDetail extends CaseSummary {
  created_by: string;
  created_at_ms: number;
  incident: { kind: string; t_start_ms: number; t_end_ms: number; superseded: boolean; trigger_event_ids: string[] };
  notes: { note_id: number; author_sub: string; author_role: string; text: string; created_at_ms: number }[];
  evidence: { event_id: string; reason: string; added_by: string; added_at_ms: number }[];
  history: {
    actor_sub: string;
    actor_role: string;
    action: string;
    from_status: string | null;
    to_status: string | null;
    detail: JsonObject;
    at_ms: number;
  }[];
  runs: RunSummary[];
  report: null | {
    report_id: string;
    run_id: string;
    valid: boolean;
    created_at_ms: number;
    body: ReportBody;
    validation: Validation;
  };
  trip: null | { trip_id: string; driver_id: string; vehicle_id: string; distance_km: number | null; status: string };
}

export type SpanKind =
  | "run_start"
  | "state"
  | "model_call"
  | "tool_call"
  | "validation"
  | "guard"
  | "human_decision"
  | "run_end";

export interface TraceSpan {
  seq: number;
  span_id: string;
  parent_span_id: string | null;
  kind: SpanKind;
  actor: string;
  status: "ok" | "error";
  model: string | null;
  usage: {
    input_tokens?: number;
    output_tokens?: number;
    cache_read_input_tokens?: number;
    cache_creation_input_tokens?: number;
  } | null;
  cost_usd: number;
  latency_ms: number;
  at_ms: number;
  input: Json;
  output: Json;
}

export interface TraceResponse {
  run: RunSummary & { case_id: string; incident_id: string };
  integrity: { ok: boolean; first_bad_seq: number | null; spans: number };
  citations: Record<string, { span_id: string; seq: number | null }>;
  spans: TraceSpan[];
}
