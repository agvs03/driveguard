import json

import anthropic
import httpx2
import pytest

from driveguard.agents.citations import validate_report
from driveguard.agents.llm import ClaudeClient, LLMError, strict_schema
from driveguard.agents.specialists import ReportDraft, TriageResult
from driveguard.mcp_servers.cases import build_cases_server
from driveguard.mcp_servers.risk import build_risk_server
from driveguard.mcp_servers.trips import build_trips_server
from tests.mcp_util import HUMAN, client, token

# ------------------------------------------------------------- strict schemas --- #

_FORBIDDEN_KEYS = {"minimum", "maximum", "minLength", "maxLength", "pattern", "minItems", "maxItems", "default",
                   "title", "exclusiveMinimum", "exclusiveMaximum"}


def _assert_strict(node, path="$"):
    if isinstance(node, dict):
        assert not (_FORBIDDEN_KEYS & set(node)), (path, _FORBIDDEN_KEYS & set(node))
        if node.get("type") == "object" or "properties" in node:
            assert node.get("additionalProperties") is False, path
        for k, v in node.items():
            if k in ("properties", "$defs"):
                for pk, pv in v.items():
                    _assert_strict(pv, f"{path}.{pk}")
            else:
                _assert_strict(v, f"{path}.{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            _assert_strict(v, f"{path}[{i}]")


async def test_every_mcp_tool_converts_to_a_strict_schema(db):
    for aud, build in (("trips", build_trips_server), ("risk", build_risk_server), ("cases", build_cases_server)):
        async with client(build(str(db)), token("human", HUMAN | {"cases:create"} - {"cases:review"}
                                                if aud != "cases" else HUMAN), aud) as c:
            for t in (await c.list_tools()).tools:
                _assert_strict(strict_schema(t.input_schema), f"{aud}.{t.name}")


@pytest.mark.parametrize("model", [TriageResult, ReportDraft])
def test_output_contracts_convert_and_keep_every_field_required(model):
    s = strict_schema(model.model_json_schema())
    _assert_strict(s)
    assert set(s["required"]) == set(s["properties"]), "optional fields would be risky under strict outputs"


def test_strict_schema_keeps_field_names_that_look_like_keywords():
    s = strict_schema({"type": "object", "properties": {"pattern": {"type": "string", "pattern": "x"},
                                                        "title": {"type": "string"}}, "required": ["pattern"]})
    assert set(s["properties"]) == {"pattern", "title"} and "pattern" not in s["properties"]["pattern"]


# ------------------------------------------------------------- ClaudeClient --- #

def _mock_client(status=200, body=None, seen=None):
    def handler(req):
        if seen is not None:
            seen["headers"], seen["body"] = dict(req.headers), json.loads(req.content)
        return httpx2.Response(status, json=body or {})
    return anthropic.AsyncAnthropic(api_key="test-key-not-real", max_retries=0,
                                    http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)))


def _msg(content, stop="end_turn", model="claude-opus-5-5", **extra):
    return {"id": "msg_1", "type": "message", "role": "assistant", "model": model, "content": content,
            "stop_reason": stop, "stop_sequence": None, **extra,
            "usage": {"input_tokens": 1200, "output_tokens": 300, "cache_read_input_tokens": 800,
                      "cache_creation_input_tokens": 0}}


TOOLS = [{"name": "trips__get_trip", "description": "d",
          "input_schema": strict_schema({"type": "object", "properties": {"trip_id": {"type": "string"}},
                                         "required": ["trip_id"]})}]


async def test_claude_request_shape():
    seen = {}
    cc = ClaudeClient(client=_mock_client(body=_msg([{"type": "text", "text": "{}"}]), seen=seen))
    await cc.create(agent="triage", system="sys", messages=[{"role": "user", "content": "go"}], tools=TOOLS,
                    output_schema=TriageResult.model_json_schema(), effort="low", max_tokens=4000)
    b, h = seen["body"], seen["headers"]
    assert b["model"] == "claude-opus-5-5" and b["max_tokens"] == 4000
    assert b["thinking"] == {"type": "adaptive"}
    assert b["output_config"]["effort"] == "low" and b["output_config"]["format"]["type"] == "json_schema"
    _assert_strict(b["output_config"]["format"]["schema"])
    assert all(t["strict"] is True for t in b["tools"])
    assert b["cache_control"] == {"type": "ephemeral"}
    assert b["fallbacks"] == "default" and h["anthropic-beta"] == "server-side-fallback-2026-07-01"
    assert "tool_choice" not in b, "forced tool choice is rejected on this model"
    assert "temperature" not in b and "budget_tokens" not in json.dumps(b)


async def test_claude_response_parsing_and_usage():
    body = _msg([{"type": "thinking", "thinking": "", "signature": "abc" * 50},
                 {"type": "tool_use", "id": "tu_1", "name": "trips__get_trip", "input": {"trip_id": "t"}}],
                stop="tool_use")
    r = await ClaudeClient(client=_mock_client(body=body)).create(
        agent="triage", system="s", messages=[{"role": "user", "content": "x"}], tools=TOOLS,
        output_schema={"type": "object", "properties": {}}, effort="low", max_tokens=100)
    assert r.stop_reason == "tool_use" and r.tool_calls[0].name == "trips__get_trip"
    assert r.usage.input_tokens == 1200 and r.usage.cache_read_input_tokens == 800
    assert len(r.raw_content) == 2, "thinking blocks are kept to be echoed back unchanged"
    assert r.trace_content[0]["signature"] == "<150 bytes>"


@pytest.mark.parametrize("status,body,kind", [
    (200, _msg([], stop="refusal", stop_details={"type": "refusal", "category": None, "explanation": None}),
     "refusal"),
    (200, _msg([{"type": "text", "text": "{\"a\":"}], stop="max_tokens"), "truncated"),
    (429, {"type": "error", "error": {"type": "rate_limit_error", "message": "slow down"}}, "rate_limited"),
    (400, {"type": "error", "error": {"type": "invalid_request_error", "message": "bad"}}, "bad_request"),
    (529, {"type": "error", "error": {"type": "overloaded_error", "message": "busy"}}, "api_529"),
])
async def test_claude_failures_become_llm_errors(status, body, kind):
    cc = ClaudeClient(client=_mock_client(status=status, body=body))
    with pytest.raises(LLMError) as e:
        await cc.create(agent="t", system="s", messages=[{"role": "user", "content": "x"}], tools=[],
                        output_schema={"type": "object", "properties": {}}, effort="low", max_tokens=10)
    assert e.value.kind == kind


# ------------------------------------------------------- citation validator --- #

EV = {
    "a1": {"event_id": "a1", "type": "accel", "event_time_ms": 10_000,
           "payload": {"ax_mean": -0.5, "ax_peak": -0.62, "ay_peak": 0.1, "az_peak": 1.0}},
    "g1": {"event_id": "g1", "type": "gps", "event_time_ms": 10_500,
           "payload": {"speed_mps": 13.4, "speed_limit_mps": 13.9, "lat": 0, "lon": 0, "heading_deg": 0}},
}
SEEN = {k: (v, "span") for k, v in EV.items()}
INC = {"t_start_ms": 9_000, "t_end_ms": 11_000}


def _report(*findings, summary="ok"):
    return {"classification": "hard_braking", "summary": summary, "contradicting_evidence": [],
            "findings": [{"claim": c, "cited_event_ids": ids, "confidence": 0.9} for c, ids in findings]}


def _codes(rep, trip=frozenset({"a1", "g1", "x9"}), seen=SEEN):
    v = validate_report(rep, trip_event_ids=set(trip), seen=seen, incident=INC, triage_classification="hard_braking")
    return {e.code for e in v.errors}, {w.code for w in v.warnings}


@pytest.mark.parametrize("claim,ids,expected", [
    ("Peak deceleration was 0.62 g.", ["a1"], set()),
    ("Peak deceleration was 0.63 g.", ["a1"], set()),                 # within tolerance
    ("Peak deceleration was 0.80 g.", ["a1"], {"E_NUMBER"}),
    ("Peak deceleration was 0.62 g.", ["g1"], {"E_NO_SOURCE"}),
    ("Speed was 13.4 m/s (48.2 km/h).", ["g1"], set()),
    ("Speed was 20 m/s.", ["g1"], {"E_NUMBER"}),
    ("Braking occurred.", ["zz"], {"E_UNKNOWN"}),
    ("Braking occurred.", ["x9"], {"E_UNGROUNDED"}),
    ("Exceeded the 0.45 g policy threshold.", ["a1"], set()),        # quoted threshold
    ("The deceleration was 0.45 g.", ["a1"], {"E_NUMBER"}),          # same number, not a threshold
])
def test_validator_cases(claim, ids, expected):
    assert _codes(_report((claim, ids)))[0] == expected


def test_summary_numbers_must_match_cited_events():
    assert _codes(_report(("Braked.", ["a1", "g1"]), summary="Peak 0.62 g at 13.4 m/s."))[0] == set()
    # 13.4 m/s is real, but no finding cites the GPS event it came from:
    assert _codes(_report(("Braked.", ["a1"]), summary="Peak 0.62 g at 13.4 m/s."))[0] == {"E_SUMMARY_NUMBER"}
    assert _codes(_report(("Braked.", ["a1"]), summary="Peak 0.95 g."))[0] == {"E_SUMMARY_NUMBER"}


def test_warnings_for_disagreement_and_irrelevance():
    far = {"a1": ({**EV["a1"], "event_time_ms": 999_999}, "s")}
    rep = _report(("Braked.", ["a1"]))
    rep["classification"] = "no_incident"
    errors, warns = _codes(rep, seen=far)
    assert not errors and {"W_CLASSIFICATION", "W_IRRELEVANT"} <= warns


async def test_workspace_header_is_sent_when_configured(monkeypatch):
    """Regression (first live run): keys not scoped to a workspace need anthropic-workspace-id."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")
    monkeypatch.setenv("ANTHROPIC_WORKSPACE_ID", "wrkspc_test_123")
    cc = ClaudeClient()
    assert cc._client.default_headers.get("anthropic-workspace-id") == "wrkspc_test_123"
    await cc.aclose()
    monkeypatch.delenv("ANTHROPIC_WORKSPACE_ID")
    cc2 = ClaudeClient()
    assert "anthropic-workspace-id" not in cc2._client.default_headers
    await cc2.aclose()


@pytest.mark.parametrize("claim,ids,expected", [
    # Verbatim first-draft sentences from the first real Claude eval that were wrongly flagged (M-17):
    ("The vehicle never stopped. Speed recovered to 13.4 m/s, which rules out a crash pattern of staying at 0 m/s.",
     ["g1"], set()),
    ("The accelerometer readings do not approach a 4 g resultant; the largest value is ax_peak -0.62 g.",
     ["a1"], set()),
    ("Vertical peaks stayed near 1 g. The vehicle recovered to 13.4 m/s instead of remaining at 0 m/s.",
     ["a1", "g1"], set()),
    # ...while a real misstatement of a policy-valued number is still caught:
    ("The vehicle was travelling at 0 m/s.", ["g1"], {"E_NUMBER"}),
])
def test_validator_does_not_flag_reasoning_about_policy_thresholds(claim, ids, expected):
    assert _codes(_report((claim, ids)))[0] == expected


@pytest.mark.parametrize("claim,ids,expected", [
    # Reports state speeds in mph for the reader; mph numbers are verified like m/s ones.
    ("Speed was 30.0 mph (13.4 m/s).", ["g1"], set()),                  # 13.4 m/s = 29.98 mph
    ("Speed was 29.5 mph.", ["g1"], set()),                             # within ~0.3 m/s
    ("The limit was 31.1 mph.", ["g1"], set()),                         # speed_limit 13.9 m/s
    ("Speed was 35 mph.", ["g1"], {"E_NUMBER"}),                        # a made-up mph figure is caught
    ("Speed was 30.0 mph.", ["a1"], {"E_NO_SOURCE"}),                   # cites no GPS event
    ("Speed was 30.0 mph (20 m/s).", ["g1"], {"E_NUMBER"}),             # right mph, wrong m/s: still caught
    ("Speed was 30 MPH.", ["g1"], set()),                               # case-insensitive
    # Policy thresholds quoted in mph or km/h are recognised after conversion (5 m/s, 0 m/s):
    ("Speed stayed above the 11.2 mph phone-distraction threshold.", ["g1"], set()),
    ("The crash criterion requires the vehicle to stay at 0 mph.", ["g1"], set()),
    ("Above the 18 km/h policy threshold.", ["g1"], set()),
    ("Speed was 11.2 mph.", ["g1"], {"E_NUMBER"}),                      # same number, not a threshold
])
def test_mph_numbers_are_verified(claim, ids, expected):
    assert _codes(_report((claim, ids)))[0] == expected


def test_mph_in_the_summary_is_verified():
    assert _codes(_report(("Braked.", ["a1", "g1"]), summary="Peak 0.62 g at 30.0 mph."))[0] == set()
    assert _codes(_report(("Braked.", ["a1", "g1"]), summary="Peak 0.62 g at 45 mph."))[0] == {"E_SUMMARY_NUMBER"}


def test_policy_constants_come_from_the_policy_text():
    from driveguard.agents.citations import _policy_constants
    c = _policy_constants()
    assert {("g", 0.45), ("g", 4.0), ("m/s", 0.0), ("m/s", 5.0), ("g", 0.40)} <= c


def test_threshold_word_anywhere_in_the_sentence_counts():
    """Verbatim from the real-Claude red-team run (M-24): 'criterion' sits 40+ characters before the number."""
    claim = ("Speed recovered to 13.4 m/s afterwards. This is inconsistent with the crash criterion, which "
             "requires the vehicle to stay at 0 m/s.")
    assert _codes(_report((claim, ["g1"])))[0] == set()
