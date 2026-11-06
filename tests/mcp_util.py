"""Helpers for MCP server tests: tokens, authenticated in-memory clients, calls."""

import asyncio
import json
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from mcp import Client
from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser

from driveguard.mcp_servers.auth import AUD, DriveGuardTokenVerifier, TokenIssuer
from driveguard.sim.generator import SimConfig, SimOutput, simulate

SECRET = "unit-test-secret-" + "k" * 40
ISSUER = TokenIssuer(SECRET)
# Seed chosen so the fixture contains every incident kind, hard negatives and driver notes
# (asserted in test_fixture_coverage so a simulator change can't silently thin it out).
MCP_SIM = SimConfig(seed=18,n_trips=10, n_drivers=4, trip_seconds=(240, 360), incident_rate=1.0,
                    negative_rate=0.5, note_rate=0.6)


def token(role: str, scopes: set[str], *, case_id: str | None = None, trip_ids: list[str] | None = None,
          aud: tuple[str, ...] = ("trips", "risk", "cases"), sub: str | None = None, ttl_s: int = 900) -> str:
    return ISSUER.issue(sub=sub or f"{role}:test", role=role, scopes=scopes, audiences=[AUD[a] for a in aud],
                        case_id=case_id, trip_ids=trip_ids, ttl_s=ttl_s)


def agent_token(case_id: str, trip_id: str, scopes: set[str] | None = None) -> str:
    return token("agent", scopes or {"trips:read", "risk:read", "cases:read", "cases:write"},
                 case_id=case_id, trip_ids=[trip_id], sub=f"agent:{case_id[:8]}")


HUMAN = {"trips:read", "risk:read", "cases:read", "cases:write", "cases:review"}
SERVICE = {"trips:read", "risk:read", "cases:read", "cases:create"}


@asynccontextmanager
async def client(server: Any, tok: str | None, aud: str):
    """In-memory client whose server task runs with `tok` as the authenticated bearer token.
    The token still goes through the real verifier (signature, audience, expiry, policy)."""
    user = None
    if tok is not None:
        access = await DriveGuardTokenVerifier(SECRET, AUD[aud]).verify_token(tok)
        assert access is not None, "test token failed verification"
        user = AuthenticatedUser(access)
    reset = auth_context_var.set(user)
    try:
        async with Client(server) as c:
            yield c
    finally:
        auth_context_var.reset(reset)


async def call(c: Client, tool: str, **args: Any) -> tuple[Any, dict[str, Any] | None]:
    """Returns (data, None) on success or (None, error_envelope) on a tool error."""
    r = await c.call_tool(tool, args)
    if r.is_error:
        return None, json.loads(r.content[0].text)
    sc = r.structured_content
    if isinstance(sc, dict) and set(sc) == {"result"}:
        sc = sc["result"]
    return sc, None


def build_golden_db(path: Path) -> SimOutput:
    """Simulate and ingest through the real pipeline, then checkpoint to a single file."""
    from tests.conftest import open_pipeline

    sim = simulate(MCP_SIM)

    async def go() -> None:
        p = await open_pipeline(path.parent, path.stem)
        await p.ingest([[e.to_json() for e in sim.events]])
        await p.store.seed_reference(sim.drivers, sim.vehicles)
        await p.close()

    asyncio.run(go())
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    return sim


def copy_db(src: Path, dst: Path) -> Path:
    with sqlite3.connect(src) as s, sqlite3.connect(dst) as d:
        s.backup(d)
    return dst
