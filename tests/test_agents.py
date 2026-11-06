"""Phase 3: orchestrator, limits, human approval, audit trace, citations.
All runs use the deterministic ScriptedClient (no API key, no cost)."""

import json
import sqlite3
from dataclasses import replace

import pytest

from driveguard.agents.budget import Limits, Price
from driveguard.agents.llm import LLMError
from driveguard.agents.report import reconstruct
from driveguard.agents.runtime import local_orchestrator
from driveguard.agents.scripted import ScriptedClient
from driveguard.agents.trace import load_spans, verify_run


def _incident(db, kind):
    with sqlite3.connect(db) as c:
        return c.execute("SELECT incident_id FROM detected_incidents WHERE kind = ? AND superseded_at_ms IS NULL "
                         "ORDER BY incident_id LIMIT 1", (kind,)).fetchone()[0]


def _conn(db):
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    return c


def _case(db, case_id):
    return dict(_conn(db).execute("SELECT * FROM cases WHERE case_id = ?", (case_id,)).fetchone())


async def _run(db, kind="crash", llm=None, guardrails=None, **limits):
    from driveguard.guardrails import ON
    async with local_orchestrator(str(db), llm or ScriptedClient(), limits=replace(Limits(), **limits),
                                  guardrails=guardrails or ON) as o:
        return await o.investigate(_incident(db, kind))


# ----------------------------------------------------------- happy path --- #

@pytest.mark.parametrize("kind", ["crash", "hard_braking", "near_miss", "phone_distraction"])
async def test_run_reaches_awaiting_approval_with_valid_citations(db, kind):
    r = await _run(db, kind)
    assert r.status == "awaiting_approval", r.reason
    assert r.validation["ok"] and r.validation["citations_checked"] > 0
    case = _case(db, r.case_id)
    assert case["status"] == "AWAITING_APPROVAL" and case["classification"] == r.report["classification"]
    with _conn(db) as c:
        attached = {x[0] for x in c.execute("SELECT event_id FROM case_evidence WHERE case_id = ?", (r.case_id,))}
        actions = [x[0] for x in c.execute("SELECT action FROM case_history WHERE case_id = ? ORDER BY id",
                                           (r.case_id,))]
    cited = {e for f in r.report["findings"] for e in f["cited_event_ids"]}
    assert cited and cited <= attached
    assert actions == ["create", "start", "evidence", "classify", "note", "submit"]
    assert "approve" not in actions, "the system must never close a case by itself"


async def test_every_model_and_tool_call_is_traced_and_chain_verifies(db):
    r = await _run(db)
    with _conn(db) as c:
        spans = load_spans(c, r.run_id)
        assert verify_run(c, r.run_id) == (True, None)
    kinds = [s["kind"] for s in spans]
    assert kinds[0] == "run_start" and kinds[-1] == "run_end"
    assert kinds.count("model_call") == r.model_calls
    agent_tool_calls = [s for s in spans if s["kind"] == "tool_call" and s["actor"] in ("triage", "evidence")]
    assert len(agent_tool_calls) == r.tool_calls
    for s in spans:
        if s["kind"] == "model_call":
            assert s["usage"] and s["model"] and "new_messages" in s["input"]
    blob = json.dumps([s["input"] for s in spans] + [s["output"] for s in spans])
    assert "eyJ" not in blob, "bearer tokens must never reach the trace"


async def test_least_privilege_each_agent_only_uses_its_allowlist(db):
    from driveguard.agents.specialists import EVIDENCE, TRIAGE
    r = await _run(db)
    with _conn(db) as c:
        spans = load_spans(c, r.run_id)
    used = {(s["actor"], s["input"]["tool"]) for s in spans if s["kind"] == "tool_call"}
    assert {t for a, t in used if a == "triage"} <= set(TRIAGE.tools)
    assert {t for a, t in used if a == "evidence"} <= set(EVIDENCE.tools)
    assert not any(a == "report_writer" for a, _ in used)
    assert all(not t.startswith("cases__") for a, t in used if a in ("triage", "evidence"))


async def test_every_citation_reconstructs_to_the_tool_call_that_returned_it(db):
    r = await _run(db, "near_miss")
    with _conn(db) as c:
        rec = reconstruct(c, r.report_id)
        spans = {s["seq"]: s for s in load_spans(c, r.run_id)}
        trip_events = {x[0] for x in c.execute("SELECT event_id FROM raw_events WHERE trip_id = ?",
                                               (r.report["trip_id"],))}
    assert rec["trace_valid"]
    for f in rec["findings"]:
        for cit in f["citations"]:
            assert cit["event"] is not None and cit["event_id"] in trip_events
            span = spans[cit["span_seq"]]
            assert span["kind"] == "tool_call" and cit["event_id"] in json.dumps(span["output"])


# --------------------------------------------------------- human review --- #

async def test_human_approval_closes_and_is_traced(db):
    async with local_orchestrator(str(db), ScriptedClient()) as o:
        r = await o.investigate(_incident(db, "crash"))
        case = await o.review(r.case_id, "approve", "Signals confirm a crash.", "alex")
    assert case["status"] == "CLOSED"
    assert case["history"][-1]["actor_sub"] == "human:alex" and case["history"][-1]["actor_role"] == "human"
    with _conn(db) as c:
        spans = load_spans(c, r.run_id)
        assert verify_run(c, r.run_id) == (True, None)
    assert spans[-1]["kind"] == "human_decision" and spans[-1]["actor"] == "human:alex"


async def test_rejection_sends_case_back_and_a_new_run_can_resubmit(db):
    async with local_orchestrator(str(db), ScriptedClient()) as o:
        inc = _incident(db, "hard_braking")
        r1 = await o.investigate(inc)
        case = await o.review(r1.case_id, "reject", "Need lateral data.", "sam")
        assert case["status"] == "INVESTIGATING"
        r2 = await o.investigate(inc)
    assert r2.case_id == r1.case_id and r2.status == "awaiting_approval" and r2.run_id != r1.run_id
    with _conn(db) as c:
        first = next(s for s in load_spans(c, r2.run_id) if s["kind"] == "model_call" and s["actor"] == "triage")
    assert "Need lateral data." in json.dumps(first["input"]), "reviewer feedback must reach the agents on rerun"


async def test_closed_or_pending_cases_are_not_reinvestigated(db):
    async with local_orchestrator(str(db), ScriptedClient()) as o:
        inc = _incident(db, "crash")
        await o.investigate(inc)
        again = await o.investigate(inc)
    assert again.status == "skipped" and "AWAITING_APPROVAL" in again.reason


# ------------------------------------------------------------ hard limits --- #

async def test_tool_loop_hits_step_limit_and_escalates(db):
    r = await _run(db, llm=ScriptedClient({"loop"}), max_tool_calls_per_agent=5)
    assert r.status == "escalated" and "tool-call limit of 5" in r.reason
    assert r.tool_calls == 5
    assert _case(db, r.case_id)["status"] == "ESCALATED"


async def test_model_call_limit(db):
    r = await _run(db, llm=ScriptedClient({"loop"}), max_model_calls_per_agent=3, max_tool_calls_per_agent=99)
    assert r.status == "escalated" and "model-call limit of 3" in r.reason and r.model_calls == 3


async def test_cost_cap_stops_before_overspending(db):
    pricey = {"scripted": Price(input=4.0, output=20.0, cache_read=0, cache_write=0)}  # Opus-5.5-like
    async with local_orchestrator(str(db), ScriptedClient({"expensive"}), limits=replace(Limits(), max_cost_usd=0.25),
                                  prices=pricey) as o:
        r = await o.investigate(_incident(db, "crash"))
    assert r.status == "escalated" and "cost cap" in r.reason
    assert 0 < r.cost_usd <= 0.25, "the pre-call worst-case check must prevent overshoot"


async def test_wall_clock_limit(db):
    r = await _run(db, max_wall_s=0.0)
    assert r.status == "escalated" and "wall-clock" in r.reason


# ------------------------------------------------ citations and guards --- #

@pytest.mark.parametrize("fault,code", [("hallucinate_citation", "E_UNKNOWN"), ("wrong_number", "E_NUMBER")])
async def test_bad_citation_is_caught_then_repaired(db, fault, code):
    r = await _run(db, "hard_braking", llm=ScriptedClient({fault}))
    assert r.status == "awaiting_approval" and r.validation["ok"]
    with _conn(db) as c:
        vals = [s for s in load_spans(c, r.run_id) if s["kind"] == "validation" and s["actor"] == "citation_validator"]
    assert [v["status"] for v in vals] == ["error", "ok"]
    assert code in {e["code"] for e in vals[0]["output"]["errors"]}


async def test_persistent_hallucination_escalates_and_keeps_invalid_report_for_humans(db):
    r = await _run(db, "hard_braking", llm=ScriptedClient({"hallucinate_always"}))
    assert r.status == "escalated" and "citation validation failed" in r.reason
    with _conn(db) as c:
        row = c.execute("SELECT valid, markdown FROM reports WHERE report_id = ?", (r.report_id,)).fetchone()
    assert row["valid"] == 0 and "Validation errors" in row["markdown"]
    assert _case(db, r.case_id)["status"] == "ESCALATED"


async def test_foreign_trip_access_is_refused_and_traced(db):
    with sqlite3.connect(db) as c:
        crash_trip = c.execute("SELECT trip_id FROM detected_incidents WHERE incident_id = ?",
                               (_incident(db, "crash"),)).fetchone()[0]
        other = c.execute("SELECT trip_id FROM trips WHERE trip_id != ? LIMIT 1", (crash_trip,)).fetchone()[0]
    from driveguard.guardrails import OFF
    # Phase 5 guardrails OFF: the case-bound token (Phase 2) must stop it at the server.
    r = await _run(db, llm=ScriptedClient({"foreign_trip"}, foreign_trip_id=other), guardrails=OFF)
    assert r.status == "awaiting_approval"
    with _conn(db) as c:
        bad = [s for s in load_spans(c, r.run_id) if s["kind"] == "tool_call" and s["status"] == "error"]
    assert bad and bad[0]["output"]["code"] == "FORBIDDEN" and bad[0]["input"]["arguments"]["trip_id"] == other
    # Guardrails ON: the unbounded request is refused by the argument limit before reaching the server.
    r2 = await _run(db, "hard_braking", llm=ScriptedClient({"foreign_trip"}, foreign_trip_id=other))
    with _conn(db) as c:
        spans = load_spans(c, r2.run_id)
    assert any(s["kind"] == "guard" and s["input"].get("guardrail") == "argument_limits" for s in spans)
    assert not any(s["kind"] == "tool_call" and s["status"] == "ok"
                   and s["input"]["arguments"].get("trip_id") == other for s in spans)


async def test_writer_cannot_use_tools(db):
    r = await _run(db, llm=ScriptedClient({"disallowed_tool"}))
    assert r.status == "awaiting_approval"
    with _conn(db) as c:
        guards = [s for s in load_spans(c, r.run_id) if s["kind"] == "guard"]
    assert guards and guards[0]["actor"] == "report_writer" and guards[0]["output"]["code"] == "FORBIDDEN"


async def test_malformed_output_is_repaired_once_then_escalates(db):
    ok = await _run(db, llm=ScriptedClient({"writer_bad_json_once"}))
    assert ok.status == "awaiting_approval"
    bad = await _run(db, "hard_braking", llm=ScriptedClient({"bad_json"}))
    assert bad.status == "escalated" and "failed validation twice" in bad.reason


async def test_llm_error_escalates(db):
    class Refuser(ScriptedClient):
        async def create(self, **kw):
            raise LLMError("refusal", "model declined (category=None)")

    r = await _run(db, llm=Refuser())
    assert r.status == "escalated" and "refusal" in r.reason and _case(db, r.case_id)["status"] == "ESCALATED"


# ------------------------------------------------------------- trace --- #

async def test_trace_is_append_only_and_tampering_is_detected(db):
    r = await _run(db)
    c = sqlite3.connect(db)
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        c.execute("UPDATE audit_trace SET output_json = '{}' WHERE run_id = ? AND seq = 3", (r.run_id,))
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        c.execute("DELETE FROM audit_trace WHERE run_id = ?", (r.run_id,))
    # An attacker with raw DB access drops the trigger and edits a tool result:
    c.execute("DROP TRIGGER audit_trace_no_update")
    c.execute("UPDATE audit_trace SET output_json = replace(output_json, '\"ax_peak\"', '\"ax_peek\"') "
              "WHERE run_id = ? AND seq = 7", (r.run_id,))
    c.commit()
    assert verify_run(c, r.run_id) == (False, 7)


async def test_offlist_tool_on_a_connected_server_is_refused(db):
    """Regression for mutation M18: allowlisting must be per tool, not per server."""
    r = await _run(db, llm=ScriptedClient({"offlist_tool"}))
    assert r.status == "awaiting_approval"
    with _conn(db) as c:
        spans = load_spans(c, r.run_id)
    guards = [s for s in spans if s["kind"] == "guard" and s["actor"] == "triage"]
    assert guards and guards[0]["input"]["tool"] == "trips__get_driver_profile"
    assert not [s for s in spans if s["kind"] == "tool_call" and s["input"]["tool"] == "trips__get_driver_profile"]


def test_redaction_of_tokens_and_keys():
    """Regression for mutation M17: secrets never flow through the trace in normal runs,
    so the end-to-end test cannot notice if redaction breaks."""
    from driveguard.agents.trace import redact
    from tests.mcp_util import token
    jwt_tok = token("human", {"trips:read"})
    key = "sk-ant-api03-" + "A" * 40
    out = redact({"note": f"bearer {jwt_tok}", "nested": [{"k": key}], "n": 3})
    blob = json.dumps(out)
    assert jwt_tok not in blob and key not in blob and "[REDACTED_TOKEN]" in blob and "[REDACTED_KEY]" in blob
    assert out["n"] == 3


async def test_investigate_all_picks_up_rejected_cases_with_feedback(db):
    """Phase 6: a rejected case used to be stranded, because --all only selected incidents without a case."""
    from driveguard.agents.runtime import pending_incidents
    from driveguard.guardrails import ON
    async with local_orchestrator(str(db), ScriptedClient(), guardrails=ON) as o:
        approved = await o.investigate(_incident(db, "crash"))
        rejected = await o.investigate(_incident(db, "hard_braking"))
        await o.review(approved.case_id, "approve", "fine", "pat")
        await o.review(rejected.case_id, "reject", "Cite the GPS speed before the brake.", "pat")
        pending = await o.db.run(pending_incidents, 100)
        assert rejected.incident_id in pending and pending[0] == rejected.incident_id, "rejected cases come first"
        assert approved.incident_id not in pending, "closed cases are never re-run"
        rerun = await o.investigate(rejected.incident_id)
        assert rerun.status == "awaiting_approval"
        spans = await o.db.run(load_spans, rerun.run_id)
    first_task = next(s for s in spans if s["kind"] == "state" and s["actor"] == "triage")["input"]["input"]
    assert "Cite the GPS speed before the brake." in first_task, "the reviewer's comment reaches the agents"
    with _conn(db) as c:
        assert rejected.incident_id not in pending_incidents(c, 100), "resubmitted: no longer pending"
