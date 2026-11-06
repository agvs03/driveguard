"""The full agent run against the three MCP servers over real HTTP (uvicorn),
with bearer tokens checked by the SDK's auth middleware on every request."""

import socket
import sqlite3
import threading
import time

import pytest
import uvicorn

from driveguard.agents.runtime import http_orchestrator
from driveguard.agents.scripted import ScriptedClient
from driveguard.mcp_servers.serve import http_app
from tests.mcp_util import SECRET

pytestmark = pytest.mark.slow


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def urls(db):
    servers, threads, out = [], [], {}
    for name in ("trips", "risk", "cases"):
        port = _free_port()
        srv = uvicorn.Server(uvicorn.Config(http_app(name, str(db), SECRET, "127.0.0.1", port), host="127.0.0.1",
                                            port=port, log_level="warning"))
        th = threading.Thread(target=srv.run, daemon=True)
        th.start()
        servers.append(srv)
        threads.append(th)
        out[name] = f"http://127.0.0.1:{port}/mcp"
    deadline = time.time() + 15
    while not all(s.started for s in servers):
        assert time.time() < deadline, "servers did not start"
        time.sleep(0.05)
    yield out
    for s in servers:
        s.should_exit = True
    for th in threads:
        th.join(timeout=10)


async def test_investigation_over_http(db, urls):
    with sqlite3.connect(db) as c:
        inc = c.execute("SELECT incident_id FROM detected_incidents WHERE kind = 'near_miss' LIMIT 1").fetchone()[0]
    import uuid

    from driveguard.obs.tracing import capture
    with capture() as ex:
        async with http_orchestrator(str(db), ScriptedClient(), urls, SECRET) as o:
            r = await o.investigate(inc)
            assert r.status == "awaiting_approval", r.reason
            case = await o.review(r.case_id, "approve", "ok", "pat")
    assert case["status"] == "CLOSED"
    # The trace crossed HTTP: server-side tool spans carry the run's trace id via the traceparent header.
    server_side = [s for s in ex.spans if s["name"] == "mcp_tool"]
    in_run = [s for s in server_side if s["trace_id"] == uuid.UUID(r.run_id).hex]
    assert in_run and {s["attrs"]["server"] for s in in_run} == {"trips", "risk", "cases"}
    client_ids = {s["span_id"] for s in ex.by_trace(uuid.UUID(r.run_id).hex) if s["name"] != "mcp_tool"}
    assert all(s["parent_span_id"] in client_ids for s in in_run), "server spans parent to a client span"


async def test_wrong_secret_cannot_do_anything(db, urls):
    with sqlite3.connect(db) as c:
        inc = c.execute("SELECT incident_id FROM detected_incidents LIMIT 1").fetchone()[0]
    from mcp.shared.exceptions import MCPError

    from driveguard.agents.orchestrator import _leaf

    with pytest.raises(Exception) as ei:  # the 401 surfaces as an MCPError inside an ExceptionGroup
        async with http_orchestrator(str(db), ScriptedClient(), urls, "attacker-secret-" + "q" * 40) as o:
            await o.investigate(inc)
    assert isinstance(_leaf(ei.value), MCPError)
    with sqlite3.connect(db) as c:
        assert c.execute("SELECT COUNT(*) FROM cases").fetchone()[0] == 0
        status, = c.execute("SELECT status FROM investigation_runs").fetchone()
        assert status == "failed", "a failed run must be closed out, not left 'running'"
        assert c.execute("SELECT COUNT(*) FROM audit_trace WHERE kind = 'run_end'").fetchone()[0] == 1
