"""Phase 6: structured logs, metrics, tracing (incl. propagation into MCP handlers) and SLO math."""

import io
import json
import logging
import sqlite3

import pytest

from driveguard.obs.context import bind, current, parse_traceparent, traceparent
from driveguard.obs.logging import configure_logging, log_event
from driveguard.obs.metrics import Registry
from driveguard.obs.slo import BY_NAME, SLOResult, evaluate, percentiles, render
from driveguard.obs.tracing import capture, span

JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ4In0.c2lnbmF0dXJlLXZhbHVl"


@pytest.fixture
def logs():
    buf = io.StringIO()
    configure_logging("DEBUG", "json", buf)
    yield lambda: [json.loads(x) for x in buf.getvalue().splitlines()]
    configure_logging("ERROR", "json")


# --------------------------------------------------------------- logging --- #

def test_log_lines_are_json_with_context_and_fields(logs):
    with bind(run_id="r1", case_id="c1", note="free text must not be bindable"):
        log_event(logging.getLogger("driveguard.x"), "thing_happened", count=3)
    line = logs()[-1]
    assert line["event"] == "thing_happened" and line["count"] == 3 and line["level"] == "info"
    assert line["run_id"] == "r1" and line["case_id"] == "c1"
    assert "note" not in line, "only allow-listed keys are bound into every line"
    assert line["ts"].endswith("Z")


def test_credentials_are_redacted_in_messages_fields_and_tracebacks(logs):
    log = logging.getLogger("driveguard.x")
    log_event(log, f"token {JWT}", header=f"Bearer {JWT}", nested={"k": "sk-ant-api03-SECRETSECRET"})
    try:
        raise ValueError(f"bad {JWT}")
    except ValueError:
        log.exception("boom")
    text = json.dumps(logs())
    assert "eyJhbGci" not in text and "SECRETSECRET" not in text
    assert "[REDACTED_TOKEN]" in text and "[REDACTED_KEY]" in text


def test_stdlib_extra_keys_become_fields(logs):
    logging.getLogger("driveguard.x").warning("plain", extra={"attempts": 2})
    assert logs()[-1]["attempts"] == 2


# --------------------------------------------------------------- metrics --- #

def test_counter_and_histogram_render_prometheus_text():
    r = Registry()
    c = r.counter("t_calls_total", "calls", ("tool",))
    h = r.histogram("t_latency_seconds", "lat", ("tool",), buckets=(0.1, 1.0))
    c.inc(tool="a")
    c.inc(2, tool="a")
    for v in (0.05, 0.5, 5.0):
        h.observe(v, tool="a")
    text = r.render()
    assert 't_calls_total{tool="a"} 3' in text
    assert 't_latency_seconds_bucket{tool="a",le="0.1"} 1' in text
    assert 't_latency_seconds_bucket{tool="a",le="1"} 2' in text  # cumulative
    assert 't_latency_seconds_bucket{tool="a",le="+Inf"} 3' in text
    assert 't_latency_seconds_count{tool="a"} 3' in text
    assert "# TYPE t_latency_seconds histogram" in text
    assert sorted(h.samples(tool="a")) == [0.05, 0.5, 5.0]


def test_metric_misuse_is_rejected():
    r = Registry()
    c = r.counter("x_total", "x", ("a",))
    with pytest.raises(ValueError):
        c.inc(b="1")  # wrong label set
    with pytest.raises(ValueError):
        c.inc(-1, a="1")
    with pytest.raises(ValueError):
        r.histogram("x_total", "x", ("a",))  # same name, different type
    assert r.counter("x_total", "x", ("a",)) is c  # re-registration with the same shape is idempotent


def test_label_values_are_escaped():
    r = Registry()
    r.counter("e_total", "e", ("v",)).inc(v='a"b\\c\nd')
    assert 'e_total{v="a\\"b\\\\c\\nd"} 1' in r.render()


# --------------------------------------------------------------- tracing --- #

def test_nested_spans_share_a_trace_and_link_parents():
    with capture() as ex:
        with span("outer", trace_id="ab" * 16) as o:
            assert current()["trace_id"] == "ab" * 16
            with span("inner", k=1) as i:
                tp = traceparent()
            with pytest.raises(RuntimeError), span("failing"):
                raise RuntimeError("x")
    by = {s["name"]: s for s in ex.spans}
    assert by["inner"]["trace_id"] == by["outer"]["trace_id"] == "ab" * 16
    assert by["inner"]["parent_span_id"] == o.span_id and by["outer"]["parent_span_id"] is None
    assert by["failing"]["status"] == "error" and "RuntimeError" in by["failing"]["error"]
    assert tp == f"00-{'ab' * 16}-{i.span_id}-01"
    assert "trace_id" not in current(), "context is restored after the span"


@pytest.mark.parametrize("value,ok", [
    ("00-" + "a" * 32 + "-" + "b" * 16 + "-01", True),
    ("00-" + "0" * 32 + "-" + "b" * 16 + "-01", False),  # all-zero trace id is invalid
    ("00-" + "a" * 31 + "-" + "b" * 16 + "-01", False),
    ("garbage", False), (None, False),
])
def test_traceparent_parsing(value, ok):
    assert (parse_traceparent(value) is not None) == ok


async def test_one_investigation_is_one_trace_from_orchestrator_into_mcp_handlers(db):
    """trace_id = run_id, and the server-side MCP tool spans are children of the gateway's
    tool_call spans: context crosses the in-process MCP transport without plumbing."""
    import uuid

    from tests.test_agents import _run

    with capture() as ex:
        r = await _run(db, "crash")
    trace_id = uuid.UUID(r.run_id).hex
    spans = ex.by_trace(trace_id)
    names = [s["name"] for s in spans]
    assert names.count("investigation") == 1 and names.count("agent") >= 3
    assert {"llm_call", "tool_call", "mcp_tool"} <= set(names)
    ids = {s["span_id"]: s for s in spans}
    for s in spans:
        if s["name"] != "investigation":
            assert s["parent_span_id"] in ids, f"{s['name']} is orphaned"
    for s in (x for x in spans if x["name"] == "mcp_tool"):
        parent = ids[s["parent_span_id"]]
        assert parent["name"] in ("tool_call",), parent["name"]
        assert s["attrs"]["code"] == "OK"
    root = next(s for s in spans if s["name"] == "investigation")
    assert root["attrs"]["status"] == "awaiting_approval" and root["attrs"]["model_calls"] == r.model_calls
    assert len(ex.spans) == len(spans), "nothing from this run escaped into another trace"


async def test_investigation_updates_metrics(db):
    from driveguard.obs import metrics as m
    from tests.test_agents import _run

    before = m.INVESTIGATIONS.value(status="awaiting_approval")
    calls_before = m.MCP_CALLS.total()
    await _run(db, "hard_braking")
    assert m.INVESTIGATIONS.value(status="awaiting_approval") == before + 1
    assert m.MCP_CALLS.total() > calls_before
    assert m.LLM_CALLS.total() > 0 and m.INVESTIGATION_LATENCY.count() > 0


def test_token_estimate_is_conservative_for_id_heavy_json():
    """M-25: chars/3 undercounted real Claude input by up to 1.84x on UUID/number-heavy JSON."""
    import uuid

    from driveguard.agents.llm import estimate_tokens
    blob = [{"event_id": str(uuid.uuid4()), "event_time_ms": 1768216407600 + i, "ax_peak": -0.6353}
            for i in range(50)]
    chars = len(json.dumps(blob))
    assert estimate_tokens(blob) >= chars / 3 * 1.84


# ------------------------------------------------------------------- SLOs --- #

def test_percentiles_are_nearest_rank():
    p = percentiles([float(x) for x in range(1, 21)])  # 1..20
    assert p["p50"] == 10 and p["p95"] == 19 and p["p99"] == 20 and p["max"] == 20
    assert percentiles([]) == {}


def test_error_budget_math():
    r = SLOResult("x", "", 0.99, good=985, total=1000)
    assert r.compliance == pytest.approx(0.985) and r.met is False
    assert r.budget_used == pytest.approx(1.5)  # 1.5% bad against a 1% budget
    assert SLOResult("x", "", 0.99, 0, 0).met is None, "no data is never reported as met"


def _slo_db(tmp_path):
    from driveguard.store.migrations import migrate
    c = sqlite3.connect(tmp_path / "s.db")
    migrate(c)
    c.execute("PRAGMA foreign_keys=OFF")
    now = 10_000_000
    runs = [("awaiting_approval", 30_000, 0.20), ("escalated", 95_000, 0.30), ("failed", 5_000, 0.01),
            ("skipped", 1, 0.0)]
    for i, (st, dur, cost) in enumerate(runs):
        c.execute("INSERT INTO investigation_runs (run_id, incident_id, status, llm, started_at_ms, ended_at_ms, "
                  "cost_usd) VALUES (?, 'i', ?, 'x', ?, ?, ?)", (f"r{i}", st, now - dur, now, cost))
    c.execute("INSERT INTO investigation_runs (run_id, incident_id, status, llm, started_at_ms) "
              "VALUES ('stuck', 'i', 'running', 'x', ?)", (now - 3_600_000,))
    c.execute("INSERT INTO investigation_runs (run_id, incident_id, status, llm, started_at_ms) "
              "VALUES ('live', 'i', 'running', 'x', ?)", (now - 1000,))
    for seq, (status, code, lat) in enumerate([("ok", None, 20), ("error", "INTERNAL", 30),
                                                ("error", "FORBIDDEN", 500), ("ok", None, 10)]):
        out = json.dumps({"code": code} if code else {})
        c.execute("INSERT INTO audit_trace (run_id, seq, span_id, kind, actor, status, input_json, output_json, "
                  "latency_ms, at_ms, prev_hash, hash) VALUES ('r0', ?, ?, 'tool_call', 'a', ?, '{}', ?, ?, 0, '', '')",
                  (seq, f"s{seq}", status, out, lat))
    c.commit()
    return c, now


def test_slo_evaluation_from_stored_data(tmp_path):
    c, now = _slo_db(tmp_path)
    res = {r.name: r for r in evaluate([c], use_metrics=False, now_ms=lambda: now)}
    comp = res["investigation-completion"]
    assert (comp.good, comp.total) == (2, 4), "skipped and in-flight runs excluded; failed and stuck are bad"
    assert "stuck" in comp.note
    lat = res["investigation-latency"]
    assert (lat.good, lat.total) == (2, 3), "95 s run misses the 90 s threshold; stuck runs are not timed"
    avail = res["mcp-tool-availability"]
    assert (avail.good, avail.total) == (3, 4), "only server faults count; FORBIDDEN is the caller's problem"
    assert (res["mcp-tool-latency"].good, res["mcp-tool-latency"].total) == (3, 4)
    assert res["ingest-latency"].total == 0 and res["ingest-latency"].met is None
    assert "**MISSED**" in render(list(res.values()))


def test_queue_slo_excludes_producer_errors(tmp_path):
    q = sqlite3.connect(tmp_path / "q.db")
    from driveguard.pipeline.broker import _SCHEMA
    q.executescript(_SCHEMA)
    for i, state in enumerate(["done"] * 997 + ["dead"] * 3):
        q.execute("INSERT INTO queue_messages (topic, body, state, visible_at_ms, enqueued_at_ms) "
                  "VALUES ('t', ?, ?, 0, 0)", (str(i), state))
    for i, reason in enumerate(["schema", "conflict", "retries_exhausted"]):
        q.execute("INSERT INTO dead_letters (msg_id, topic, body, reason, attempts, dead_at_ms) "
                  "VALUES (?, 't', 'b', ?, 1, 0)", (2000 + i, reason))
    q.commit()
    r = next(x for x in evaluate([], [q], use_metrics=False) if x.name == "ingest-availability")
    assert (r.good, r.total) == (997, 998) and r.met is False
    assert BY_NAME["ingest-availability"].objective == 0.999
