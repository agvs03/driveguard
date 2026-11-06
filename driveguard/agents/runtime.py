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


def http_orchestrator(db_path: str, llm: LLMClient, urls: dict[str, str], secret: str, *,
                      limits: Limits = Limits(), prices: dict[str, Any] | None = None) -> Orchestrator:
    return Orchestrator(db_path, HttpConnector(urls), TokenIssuer(secret), llm, limits=limits, prices=prices)
