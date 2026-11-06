"""End-to-end over real HTTP: bearer auth is enforced by the SDK middleware,
not by our test helper. Runs uvicorn on a free port in a background thread."""

import socket
import threading
import time

import httpx2
import pytest
import uvicorn
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from driveguard.mcp_servers.serve import http_app
from tests.mcp_util import SECRET, agent_token, call, token

pytestmark = pytest.mark.slow

INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                   "clientInfo": {"name": "t", "version": "0"}}}
HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def trips_url(db):
    port = _free_port()
    app = http_app("trips", str(db), SECRET, "127.0.0.1", port)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    deadline = time.time() + 10
    while not server.started:
        if time.time() > deadline:
            raise RuntimeError("server did not start")
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}/mcp"
    server.should_exit = True
    th.join(timeout=10)


@pytest.mark.parametrize("auth", [None, "Bearer not-a-jwt", "Basic dXNlcjpwYXNz"])
def test_requests_without_valid_bearer_get_401(trips_url, auth):
    headers = dict(HEADERS)
    if auth:
        headers["Authorization"] = auth
    r = httpx2.post(trips_url, json=INIT, headers=headers)
    assert r.status_code == 401
    assert "bearer" in r.headers.get("www-authenticate", "").lower()


def test_token_for_another_server_gets_401(trips_url):
    tok = token("human", {"cases:read"}, aud=("cases",))
    r = httpx2.post(trips_url, json=INIT, headers={**HEADERS, "Authorization": f"Bearer {tok}"})
    assert r.status_code == 401


async def test_valid_token_works_and_binding_still_applies(trips_url, sim):
    trips = list(dict.fromkeys(str(e.trip_id) for e in sim.events))
    tok = agent_token("case-http", trips[0])
    async with httpx2.AsyncClient(headers={"Authorization": f"Bearer {tok}"}) as http:
        async with Client(streamable_http_client(trips_url, http_client=http)) as c:
            data, err = await call(c, "get_trip", trip_id=trips[0])
            assert err is None and data["trip_id"] == trips[0]
            _, err = await call(c, "get_trip", trip_id=trips[1])
            assert err["code"] == "FORBIDDEN"
