"""Wiring helpers: build an Orchestrator against in-process or HTTP MCP servers."""

from __future__ import annotations

import secrets
from typing import Any

from driveguard.agents.budget import Limits
from driveguard.agents.gateway import HttpConnector, InMemoryConnector
from driveguard.agents.llm import LLMClient
from driveguard.agents.orchestrator import Orchestrator
from driveguard.guardrails import ON, Guardrails
from driveguard.mcp_servers.auth import AUD, DriveGuardTokenVerifier, TokenIssuer
from driveguard.mcp_servers.cases import build_cases_server
from driveguard.mcp_servers.risk import build_risk_server
from driveguard.mcp_servers.trips import build_trips_server


def local_orchestrator(db_path: str, llm: LLMClient, *, secret: str | None = None, limits: Limits = Limits(),
                       prices: dict[str, Any] | None = None, guardrails: Guardrails = ON) -> Orchestrator:
    """All three MCP servers in-process. With no secret given, a random one is
    generated per process: tokens never leave the process, so nothing is persisted."""
    secret = secret or secrets.token_urlsafe(48)
    servers = {"trips": build_trips_server(db_path, guardrails=guardrails), "risk": build_risk_server(db_path),
               "cases": build_cases_server(db_path)}
    verifiers = {k: DriveGuardTokenVerifier(secret, AUD[k]) for k in servers}
    return Orchestrator(db_path, InMemoryConnector(servers, verifiers), TokenIssuer(secret), llm, limits=limits,
                        prices=prices, guardrails=guardrails)


def pending_incidents(conn: Any, limit: int) -> list[str]:
    """Incidents that need an investigation run, oldest first:
      * active incidents with no case yet;
      * cases a human REJECTED (status INVESTIGATING after a reject), so the rerun gets the reviewer's
        comment as feedback. Without this, a rejected case was never picked up again (Phase 6 review);
      * cases left INVESTIGATING by a run that died (no run in progress for them).
    A case with a run still marked 'running' less than 10 minutes ago is skipped (someone is on it)."""
    import time
    stale = int(time.time() * 1000) - 10 * 60_000
    rows = conn.execute(
        "SELECT i.incident_id FROM detected_incidents i LEFT JOIN cases k USING (incident_id) "
        "WHERE i.superseded_at_ms IS NULL AND (k.case_id IS NULL OR (k.status = 'INVESTIGATING' AND NOT EXISTS ("
        "  SELECT 1 FROM investigation_runs r WHERE r.case_id = k.case_id AND r.status = 'running' "
        "  AND r.started_at_ms > ?))) "
        "ORDER BY (k.case_id IS NOT NULL) DESC, i.t_start_ms LIMIT ?", (stale, limit)).fetchall()
    return [r[0] for r in rows]


def http_orchestrator(db_path: str, llm: LLMClient, urls: dict[str, str], secret: str, *,
                      limits: Limits = Limits(), prices: dict[str, Any] | None = None) -> Orchestrator:
    return Orchestrator(db_path, HttpConnector(urls), TokenIssuer(secret), llm, limits=limits, prices=prices)
