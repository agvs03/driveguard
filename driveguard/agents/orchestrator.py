"""Orchestrator: a deterministic state machine (code, not an LLM).

    OPEN -> TRIAGE -> EVIDENCE -> REPORT -> VALIDATE (-> one REPAIR) -> CASE WRITES -> AWAITING_APPROVAL
                         any failure (budget, LLM error, refusal, invalid output,
                         citation errors after repair) -> ESCALATED with a reason

It mints a separate least-privilege, case-bound token for each actor:
  service:orchestrator  create the case (cases:create; unbound service token)
  orchestrator          case writes only (cases:read/write), bound to case + trip
  triage / evidence     read tools only (trips:read, risk:read), bound to case + trip
  report_writer         no token at all: it has no tools
The orchestrator never closes a case: closing needs a human reviewer token
(`review_case`, scope cases:review, role human).
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from driveguard.agents.budget import Budget, BudgetExceeded, Limits
from driveguard.agents.citations import validate_report
from driveguard.agents.gateway import Connector, ToolGateway
from driveguard.agents.llm import LLMClient, LLMError
from driveguard.agents.report import render_markdown
from driveguard.agents.specialists import (EVIDENCE, TRIAGE, WRITER, AgentFailure, EvidenceResult,
                                           InvestigationReport, ReportDraft, TriageResult, run_agent)
from driveguard.agents.trace import Span, Trace, events_seen, load_spans
from driveguard.mcp_servers.auth import AUD, TokenIssuer
from driveguard.obs.context import annotate, bind
from driveguard.obs.logging import log_event
from driveguard.obs.metrics import (ESCALATIONS, GUARD_BLOCKS, INVESTIGATION_COST, INVESTIGATION_LATENCY,
                                    INVESTIGATIONS)
from driveguard.obs.tracing import span
from driveguard.store.db import Database, transaction

AGENT_READ = {"trips:read", "risk:read"}
ORCH_WRITE = {"cases:read", "cases:write"}
SERVICE = {"cases:read", "cases:create"}
log = logging.getLogger("driveguard.orchestrator")
CASE_WRITE_TOOLS = ["cases__get_case", "cases__start_investigation", "cases__attach_evidence",
                    "cases__set_classification", "cases__add_note", "cases__submit_for_approval", "cases__escalate"]


@dataclass
class RunOutcome:
    run_id: str
    incident_id: str
    case_id: str | None
    status: str  # awaiting_approval | escalated | skipped | failed
    reason: str = ""
    report_id: str | None = None
    report: dict[str, Any] | None = None
    validation: dict[str, Any] | None = None
    cost_usd: float = 0.0
    model_calls: int = 0
    tool_calls: int = 0
    latency_s: float = 0.0
    triage: dict[str, Any] | None = None
    extra: dict[str, Any] = field(default_factory=dict)


class Orchestrator:
    def __init__(self, db_path: str, connector: Connector, issuer: TokenIssuer, llm: LLMClient, *,
                 limits: Limits = Limits(), prices: dict[str, Any] | None = None, guardrails: Any = None,
                 now_ms: Callable[[], int] = lambda: int(time.time() * 1000)) -> None:
        self.db = Database(db_path)
        self.connector, self.issuer, self.llm, self.limits = connector, issuer, llm, limits
        self.prices = prices
        from driveguard.guardrails import ON
        self.guardrails = guardrails if guardrails is not None else ON
        self.now_ms = now_ms

    async def __aenter__(self) -> "Orchestrator":
        from driveguard.store.migrations import migrate
        await self.db.connect(init=migrate)
        return self

    async def __aexit__(self, *exc: Any) -> None:
        close = getattr(self.llm, "aclose", None)
        if close is not None:  # close the HTTP client inside the event loop that opened it
            await close()
        await self.db.close()

    # ------------------------------------------------------------ tokens --- #
    def _token(self, sub: str, role: str, scopes: set[str], aud: list[str], case_id: str | None = None,
               trip_id: str | None = None) -> str:
        return self.issuer.issue(sub=sub, role=role, scopes=scopes, audiences=[AUD[a] for a in aud],
                                 case_id=case_id, trip_ids=[trip_id] if trip_id else None, ttl_s=900)

    # --------------------------------------------------------------- run --- #
    async def investigate(self, incident_id: str) -> RunOutcome:
        run_id = str(uuid.uuid4())
        # trace_id = run_id's hex: telemetry for this run joins the audit trace on one key.
        with bind(run_id=run_id, incident_id=incident_id, component="orchestrator"),                 span("investigation", trace_id=uuid.UUID(run_id).hex, llm=self.llm.name) as sp:
            log_event(log, "investigation_started", model=self.llm.model)
            out = await self._investigate_run(run_id, incident_id)
            sp.set(status=out.status, cost_usd=out.cost_usd, model_calls=out.model_calls, tool_calls=out.tool_calls)
            if out.status in ("escalated", "failed"):
                sp.error(out.status)
            return out

    async def _investigate_run(self, run_id: str, incident_id: str) -> RunOutcome:
        t0 = time.perf_counter()
        budget = Budget(limits=self.limits, **({"prices": self.prices} if self.prices else {}))
        trace = Trace(self.db, run_id, now_ms=self.now_ms)
        out = RunOutcome(run_id=run_id, incident_id=incident_id, case_id=None, status="escalated")
        await self.db.run(_insert_run, run_id, incident_id, self.llm.name + ":" + self.llm.model, self.now_ms())
        root = await trace.record("run_start", "orchestrator",
                                  {"incident_id": incident_id, "llm": self.llm.name, "model": self.llm.model,
                                   "limits": self.limits.__dict__}, {})
        try:
            return await self._investigate(incident_id, out, trace, root, budget, t0)
        except BaseException as exc:
            # Infrastructure failure (e.g. MCP auth/transport). Never leave a run 'running' with no run_end.
            out.status, out.reason = "failed", f"{type(_leaf(exc)).__name__}: {str(_leaf(exc))[:300]}"
            await self._finish(trace, root, out, budget, t0)
            raise

    async def _investigate(self, incident_id: str, out: RunOutcome, trace: Trace, root: Span, budget: Budget,
                           t0: float) -> RunOutcome:
        run_id = trace.run_id

        # 1) Open (or re-open) the case with a service token.
        svc = self._token("service:orchestrator", "service", SERVICE, ["cases"])
        async with ToolGateway.open(self.connector, svc, "service:orchestrator", ["cases__create_case"],
                                    trace) as gw:
            r = await gw.call("cases__create_case", {"incident_id": incident_id}, parent=root, count_budget=False)
        if r.is_error:
            out.reason = f"could not open case: {r.data}"
            out.status = "skipped"
            return await self._finish(trace, root, out, budget, t0)
        case = r.data["case"]
        out.case_id = trace.case_id = case["case_id"]
        annotate(case_id=out.case_id)
        trip_id = case["trip_id"]
        if case["status"] not in ("OPEN", "INVESTIGATING"):
            out.status, out.reason = "skipped", f"case is {case['status']}"
            return await self._finish(trace, root, out, budget, t0)

        orch_tok = self._token(f"orchestrator:{run_id[:8]}", "agent", ORCH_WRITE, ["cases"], case["case_id"], trip_id)
        read_tok = lambda who: self._token(f"agent:{who}:{run_id[:8]}", "agent", AGENT_READ,  # noqa: E731
                                           ["trips", "risk"], case["case_id"], trip_id)
        async with ToolGateway.open(self.connector, orch_tok, "orchestrator", CASE_WRITE_TOOLS, trace) as cases:
            if case["status"] == "OPEN":
                await cases.call("cases__start_investigation", {"case_id": case["case_id"]}, parent=root,
                                 count_budget=False)
            try:
                await self._pipeline(out, case, trip_id, read_tok, cases, trace, root, budget)
            except Exception as exc:  # noqa: BLE001 - classified below, anything else re-raised
                # Errors raised inside an open MCP session come back wrapped in an
                # ExceptionGroup by anyio's task group (DECISIONS M-9): unwrap first.
                e = _leaf(exc)
                if isinstance(e, BudgetExceeded):
                    await self._escalate(out, cases, trace, root, f"budget: {e.reason}")
                elif isinstance(e, AgentFailure):
                    await self._escalate(out, cases, trace, root, f"agent failure: {e}")
                elif isinstance(e, LLMError):
                    await self._escalate(out, cases, trace, root, f"llm error: {e}")
                else:
                    await self._escalate(out, cases, trace, root, f"internal error: {type(e).__name__}")
                    raise
        return await self._finish(trace, root, out, budget, t0)

    async def _pipeline(self, out: RunOutcome, case: dict[str, Any], trip_id: str, read_tok: Callable[[str], str],
                        cases: ToolGateway, trace: Trace, root: Span, budget: Budget) -> None:
        case_id, incident_id = case["case_id"], case["incident_id"]
        task = {"case_id": case_id, "incident_id": incident_id, "trip_id": trip_id,
                "detected_kind": case["incident"]["kind"]}
        # A human rejected an earlier report: pass their comments on, or the rerun can't act on them.
        feedback = [h["detail"].get("comment") for h in case.get("history", [])
                    if h["action"] == "reject" and h["actor_role"] == "human"]
        if feedback:
            task["reviewer_feedback"] = feedback

        async with ToolGateway.open(self.connector, read_tok("triage"), "triage", list(TRIAGE.tools), trace,
                                    budget, self.guardrails) as gw:
            triage: TriageResult = await run_agent(TRIAGE, task, llm=self.llm, gateway=gw, budget=budget,
                                                   trace=trace, parent=root)  # type: ignore[assignment]
        out.triage = triage.model_dump()
        async with ToolGateway.open(self.connector, read_tok("evidence"), "evidence", list(EVIDENCE.tools), trace,
                                    budget, self.guardrails) as gw:
            evidence: EvidenceResult = await run_agent(EVIDENCE, {**task, "triage": triage.model_dump()},
                                                       llm=self.llm, gateway=gw, budget=budget, trace=trace,
                                                       parent=root)  # type: ignore[assignment]

        # Evidence pack: only event records that tools actually returned in this run.
        spans = await self.db.run(load_spans, trace.run_id)
        seen = events_seen(spans)
        notes = [ev for ev, _ in seen.values() if ev.get("type") == "driver_note"]
        flagged = [n for n in notes if n.get("injection_signals")]
        if flagged and self.guardrails.sanitize_untrusted_text:
            GUARD_BLOCKS.inc(guard="untrusted_text")
            # Tell the human reviewer, in the case itself, that someone tried to steer the agents.
            sig = sorted({x for n in flagged for x in n["injection_signals"]})
            await trace.record("guard", "orchestrator", {"guardrail": "untrusted_text"},
                               {"flagged_notes": [n["event_id"] for n in flagged], "signals": sig}, parent=root)
            await _ok(cases.call("cases__add_note", {"case_id": case_id, "text": (
                f"[guardrail] {len(flagged)} driver note(s) contain instruction-like text ({', '.join(sig)}). "
                "They were treated as untrusted data; review them before accepting the report.")},
                parent=root, count_budget=False))
        wanted = list(dict.fromkeys(triage.cited_event_ids + [i.event_id for i in evidence.items]))
        pack = {**task, "triage": triage.model_dump(), "evidence": evidence.model_dump(),
                "events": [seen[e][0] for e in wanted if e in seen]}

        trip_events = set(await self.db.run(
            lambda c: [r[0] for r in c.execute("SELECT event_id FROM raw_events WHERE trip_id = ?", (trip_id,))]))
        incident = await self.db.run(
            lambda c: dict(c.execute("SELECT t_start_ms, t_end_ms FROM detected_incidents WHERE incident_id = ?",
                                     (incident_id,)).fetchone()))

        draft: ReportDraft | None = None
        validation = None
        for attempt in range(2):
            writer_input = pack if attempt == 0 else {
                **pack, "previous_draft": draft.model_dump() if draft else None,
                "validation_errors": validation.feedback() if validation else "",
                "instruction": "Fix every listed error. Cite only ids present in `events`; copy numbers exactly."}
            draft = await run_agent(WRITER, writer_input, llm=self.llm, gateway=None, budget=budget, trace=trace,
                                    parent=root)  # type: ignore[assignment]
            spans = await self.db.run(load_spans, trace.run_id)
            validation = validate_report(draft.model_dump(), trip_event_ids=trip_events, seen=events_seen(spans),
                                         incident=incident, triage_classification=triage.classification)
            await trace.record("validation", "citation_validator", {"attempt": attempt}, validation.as_dict(),
                               status="ok" if validation.ok else "error", parent=root)
            if validation.ok:
                break

        report = InvestigationReport(**draft.model_dump(), report_id=str(uuid.uuid4()), case_id=case_id,
                                     incident_id=incident_id, trip_id=trip_id, run_id=trace.run_id,
                                     risk_score=evidence.risk_score, risk_score_id=evidence.risk_score_id)
        checks = validation.as_dict()
        guard = None
        if validation.ok and self.guardrails.output_guard:
            from driveguard.guardrails.output import check_report
            guard = await self.db.run(check_report, report.model_dump(), incident_id, notes)
            await trace.record("guard", "output_guard", {"report_id": report.report_id}, guard.as_dict(),
                               status="ok" if guard.ok else "error", parent=root)
            checks = {**checks, "ok": guard.ok, "guardrails": guard.as_dict()}
        await self.db.run(_save_report, report, checks, self.now_ms())
        out.report_id, out.report, out.validation = report.report_id, report.model_dump(), checks
        if not validation.ok:
            raise AgentFailure("report_writer", "citation validation failed after one repair: "
                               + "; ".join(f"{i.code}@{i.where}" for i in validation.errors[:5]))
        if guard is not None and not guard.ok:
            GUARD_BLOCKS.inc(guard="output_guard")
            raise AgentFailure("output_guard", "report blocked: " + "; ".join(
                f"{v['code']}: {v['detail']}" for v in guard.violations)[:400])

        # Case writes (orchestrator token; traced as tool calls).
        cited = list(dict.fromkeys(e for f in report.findings + report.contradicting_evidence
                                   for e in f.cited_event_ids))
        await _ok(cases.call("cases__attach_evidence", {"case_id": case_id, "event_ids": cited[:50],
                                                         "reason": f"cited in report {report.report_id}"},
                             parent=root, count_budget=False))
        cur = await _ok(cases.call("cases__get_case", {"case_id": case_id}, parent=root, count_budget=False))
        cur = await _ok(cases.call("cases__set_classification",
                                   {"case_id": case_id, "classification": report.classification,
                                    "confidence": report.confidence, "expected_version": cur["version"]},
                                   parent=root, count_budget=False))
        await _ok(cases.call("cases__add_note", {"case_id": case_id, "text": (
            f"[report {report.report_id}] {report.summary}")[:4000]}, parent=root, count_budget=False))
        cur = await _ok(cases.call("cases__get_case", {"case_id": case_id}, parent=root, count_budget=False))
        await _ok(cases.call("cases__submit_for_approval", {"case_id": case_id, "expected_version": cur["version"]},
                             parent=root, count_budget=False))
        out.status = "awaiting_approval"

    async def _escalate(self, out: RunOutcome, cases: ToolGateway, trace: Trace, root: Span, reason: str) -> None:
        out.status, out.reason = "escalated", reason[:480]
        await trace.record("guard", "orchestrator", {"action": "escalate"}, {"reason": out.reason},
                           status="error", parent=root)
        await cases.call("cases__escalate", {"case_id": out.case_id, "reason": out.reason}, parent=root,
                         count_budget=False)

    async def _finish(self, trace: Trace, root: Span, out: RunOutcome, budget: Budget, t0: float) -> RunOutcome:
        out.cost_usd = round(budget.spent_usd, 6)
        out.model_calls, out.tool_calls = budget.total_model_calls, budget.total_tool_calls
        out.latency_s = round(time.perf_counter() - t0, 3)
        summary = {"status": out.status, "reason": out.reason, "case_id": out.case_id, "report_id": out.report_id,
                   "cost_usd": out.cost_usd, "model_calls": out.model_calls, "tool_calls": out.tool_calls,
                   "latency_s": out.latency_s}
        await trace.record("run_end", "orchestrator", {}, summary, parent=root)
        await self.db.run(_end_run, out, self.now_ms())
        INVESTIGATIONS.inc(status=out.status)
        if out.status != "skipped":
            INVESTIGATION_LATENCY.observe(out.latency_s)
            INVESTIGATION_COST.observe(out.cost_usd)
        if out.status == "escalated":
            ESCALATIONS.inc(cause=escalation_cause(out.reason))
        log_event(log, "investigation_finished", logging.WARNING if out.status == "failed" else logging.INFO,
                  status=out.status, cause=escalation_cause(out.reason) if out.status == "escalated" else None,
                  cost_usd=out.cost_usd, latency_s=out.latency_s, model_calls=out.model_calls,
                  tool_calls=out.tool_calls)
        return out

    # ------------------------------------------------------ human review --- #
    async def review(self, case_id: str, decision: str, comment: str, reviewer: str) -> dict[str, Any]:
        """Human-in-the-loop approval. Mints a human token for `reviewer` (in production this
        would come from SSO), calls review_case, and appends a human_decision span to the run."""
        run_id = await self.db.run(lambda c: (c.execute(
            "SELECT run_id FROM investigation_runs WHERE case_id = ? ORDER BY started_at_ms DESC, rowid DESC "
            "LIMIT 1", (case_id,)).fetchone() or [None])[0])
        trace = Trace(self.db, run_id or f"review-{uuid.uuid4()}", case_id=case_id, now_ms=self.now_ms)
        tok = self._token(f"human:{reviewer}", "human", {"cases:read", "cases:review"}, ["cases"])
        async with ToolGateway.open(self.connector, tok, f"human:{reviewer}", ["cases__get_case", "cases__review_case"],
                                    trace) as gw:
            case = await _ok(gw.call("cases__get_case", {"case_id": case_id}, count_budget=False))
            r = await gw.call("cases__review_case", {"case_id": case_id, "decision": decision, "comment": comment,
                                                     "expected_version": case["version"]}, count_budget=False)
        await trace.record("human_decision", f"human:{reviewer}", {"decision": decision, "comment": comment},
                           r.data if r.data is not None else {}, status="error" if r.is_error else "ok")
        if r.is_error:
            raise RuntimeError(f"review failed: {r.data}")
        return r.data


_EXPECTED = (BudgetExceeded, AgentFailure, LLMError)


def escalation_cause(reason: str) -> str:
    """Map a free-text escalation reason to a small closed set (a metric label must not be free text)."""
    r = reason.lower()
    for needle, cause in (("output_guard", "output_guard"), ("citation validation", "citation_validation"),
                          ("possible manipulation", "denials"), ("budget", "budget"), ("llm error", "llm_error"),
                          ("agent failure", "agent_failure"), ("internal error", "internal")):
        if needle in r:
            return cause
    return "other"


def _leaf(exc: BaseException) -> BaseException:
    """Return the first expected error inside (possibly nested) exception groups."""
    try:
        group = BaseExceptionGroup  # type: ignore[name-defined]  # Python 3.11+
    except NameError:
        from exceptiongroup import BaseExceptionGroup as group  # backport used by anyio on 3.10
    if isinstance(exc, group):
        leaves = [_leaf(e) for e in exc.exceptions]
        return next((e for e in leaves if isinstance(e, _EXPECTED)), leaves[0])
    return exc


async def _ok(awaitable: Any) -> Any:
    r = await awaitable
    if r.is_error:
        raise AgentFailure("orchestrator", f"case write failed: {r.data}")
    return r.data


def _insert_run(conn: sqlite3.Connection, run_id: str, incident_id: str, llm: str, now: int) -> None:
    with transaction(conn):
        conn.execute("INSERT INTO investigation_runs (run_id, incident_id, status, llm, started_at_ms) "
                     "VALUES (?, ?, 'running', ?, ?)", (run_id, incident_id, llm, now))


def _end_run(conn: sqlite3.Connection, out: RunOutcome, now: int) -> None:
    with transaction(conn):
        conn.execute("UPDATE investigation_runs SET case_id = ?, status = ?, ended_at_ms = ?, cost_usd = ?, "
                     "model_calls = ?, tool_calls = ?, outcome_json = ? WHERE run_id = ?",
                     (out.case_id, out.status, now, out.cost_usd, out.model_calls, out.tool_calls,
                      json.dumps({"reason": out.reason, "report_id": out.report_id, "latency_s": out.latency_s}),
                      out.run_id))


def _save_report(conn: sqlite3.Connection, report: InvestigationReport, validation: dict[str, Any], now: int) -> None:
    with transaction(conn):
        conn.execute("INSERT INTO reports (report_id, case_id, run_id, valid, report_json, validation_json, markdown, "
                     "created_at_ms) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                     (report.report_id, report.case_id, report.run_id, int(validation["ok"]),
                      report.model_dump_json(), json.dumps(validation), render_markdown(report.model_dump(), validation),
                      now))
