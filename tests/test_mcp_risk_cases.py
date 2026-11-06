import sqlite3

import pytest

from driveguard.mcp_servers.cases import build_cases_server
from driveguard.mcp_servers.risk import build_risk_server
from tests.mcp_util import HUMAN, SERVICE, agent_token, call, client, token


def _incidents(db):
    conn = sqlite3.connect(db)
    return conn.execute("SELECT incident_id, trip_id, kind, trigger_event_ids_json FROM detected_incidents "
                        "WHERE superseded_at_ms IS NULL ORDER BY incident_id").fetchall()


# ------------------------------- risk-server ------------------------------- #

async def test_score_and_explain_round_trip(db):
    _, trip, _, _ = _incidents(db)[0]
    srv = build_risk_server(str(db))
    async with client(srv, agent_token("c", trip), "risk") as c:
        rs, err = await call(c, "score_trip", trip_id=trip)
        assert err is None
        assert rs["score"] == pytest.approx(sum(f["contribution"] for f in rs["factors"]), abs=1e-6)
        again, _ = await call(c, "score_trip", trip_id=trip)
        assert again == rs, "deterministic"
        ex, err = await call(c, "explain_score", score_id=rs["score_id"])
        assert err is None and ex["score"] == rs and len(ex["explanation"]) == len(rs["factors"])
        w, _ = await call(c, "get_weights")
        assert w["version"] == "weights-v1"


async def test_explain_score_respects_binding(db):
    incs = _incidents(db)
    trip_a = incs[0][1]
    trip_b = next(i[1] for i in incs if i[1] != trip_a)
    srv = build_risk_server(str(db))
    async with client(srv, token("human", HUMAN), "risk") as c:
        rs_b, _ = await call(c, "score_trip", trip_id=trip_b)
    async with client(srv, agent_token("c", trip_a), "risk") as c:
        _, err = await call(c, "explain_score", score_id=rs_b["score_id"])
        assert err["code"] == "FORBIDDEN"
        _, err = await call(c, "score_trip", trip_id=trip_b)
        assert err["code"] == "FORBIDDEN"
        _, err = await call(c, "score_trip", trip_id=trip_a, weights_version="weights-v9")
        assert err["code"] == "INVALID_ARGUMENT"


# ------------------------------- cases-server ------------------------------ #

async def test_full_lifecycle_with_human_approval(db):
    inc_id, trip, kind, triggers = _incidents(db)[0]
    srv = build_cases_server(str(db))
    async with client(srv, token("service", SERVICE, sub="service:orchestrator"), "cases") as c:
        res, err = await call(c, "create_case", incident_id=inc_id)
        assert err is None and res["created"] is True
        res2, _ = await call(c, "create_case", incident_id=inc_id)
        assert res2["created"] is False and res2["case"]["case_id"] == res["case"]["case_id"]
    case_id = res["case"]["case_id"]
    first_trigger = res["case"]["incident"]["trigger_event_ids"][0]

    async with client(srv, agent_token(case_id, trip), "cases") as c:
        case, _ = await call(c, "start_investigation", case_id=case_id)
        ev, err = await call(c, "attach_evidence", case_id=case_id, event_ids=[first_trigger], reason="trigger")
        assert err is None and ev["added"] == 1
        note, err = await call(c, "add_note", case_id=case_id, text="Decel 0.6 g at trigger.")
        assert err is None
        case, _ = await call(c, "get_case", case_id=case_id)
        case, err = await call(c, "set_classification", case_id=case_id, classification=kind, confidence=0.85,
                               expected_version=case["version"])
        assert err is None
        case, err = await call(c, "submit_for_approval", case_id=case_id, expected_version=case["version"])
        assert err is None and case["status"] == "AWAITING_APPROVAL"
        # The agent tries to close its own case: no scope.
        _, err = await call(c, "review_case", case_id=case_id, decision="approve", comment="lgtm",
                            expected_version=case["version"])
        assert err["code"] == "FORBIDDEN" and "cases:review" in err["message"]

    async with client(srv, token("human", HUMAN, sub="human:reviewer-1"), "cases") as c:
        case, err = await call(c, "review_case", case_id=case_id, decision="approve", comment="Evidence checks out.",
                               expected_version=case["version"])
    assert err is None and case["status"] == "CLOSED"
    assert [(h["action"], h["actor_role"]) for h in case["history"]] == [
        ("create", "service"), ("start", "agent"), ("evidence", "agent"), ("note", "agent"),
        ("classify", "agent"), ("submit", "agent"), ("approve", "human")]
    assert case["history"][-1]["actor_sub"] == "human:reviewer-1"


async def test_agent_cannot_touch_another_case(db):
    (inc_a, trip_a, _, _), (inc_b, trip_b, _, _) = _incidents(db)[:2]
    srv = build_cases_server(str(db))
    async with client(srv, token("service", SERVICE), "cases") as c:
        a = (await call(c, "create_case", incident_id=inc_a))[0]["case"]["case_id"]
        b = (await call(c, "create_case", incident_id=inc_b))[0]["case"]["case_id"]
    async with client(srv, agent_token(a, trip_a), "cases") as c:
        listed, _ = await call(c, "list_cases")
        assert [x["case_id"] for x in listed] == [a], "list is filtered to the bound case"
        for tool, args in [("get_case", {}), ("add_note", {"text": "hi"}),
                           ("start_investigation", {}), ("escalate", {"reason": "x"})]:
            _, err = await call(c, tool, case_id=b, **args)
            assert err["code"] == "FORBIDDEN", tool
        _, err = await call(c, "create_case", incident_id=inc_b)
        assert err["code"] == "FORBIDDEN"


async def test_stale_version_is_a_retryable_conflict(db):
    inc, trip, _, _ = _incidents(db)[0]
    srv = build_cases_server(str(db))
    async with client(srv, token("service", SERVICE), "cases") as c:
        case_id = (await call(c, "create_case", incident_id=inc))[0]["case"]["case_id"]
    async with client(srv, agent_token(case_id, trip), "cases") as c:
        await call(c, "start_investigation", case_id=case_id)
        _, err = await call(c, "set_classification", case_id=case_id, classification="crash", confidence=0.5,
                            expected_version=1)
    assert err["code"] == "CONFLICT" and err["retryable"] is True


async def test_case_tools_have_strict_schemas(db):
    srv = build_cases_server(str(db))
    async with client(srv, token("human", HUMAN), "cases") as c:
        tools = (await c.list_tools()).tools
        for t in tools:
            assert t.input_schema.get("additionalProperties") is False, t.name
        _, err = await call(c, "set_classification", case_id="00000000-0000-4000-8000-000000000000",
                            classification="speeding", confidence=2, expected_version=0)
    assert err["code"] == "INVALID_ARGUMENT"
    for field in ("classification", "confidence", "expected_version"):
        assert field in err["message"]
