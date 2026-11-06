"""Run an MCP server over streamable HTTP with bearer-token auth.

The HMAC secret comes only from the DRIVEGUARD_TOKEN_SECRET environment
variable. There is deliberately no default, so a forgotten secret fails closed.
"""

import os
from typing import Any

from driveguard.mcp_servers.auth import AUD, DriveGuardTokenVerifier

SECRET_ENV = "DRIVEGUARD_TOKEN_SECRET"
DEFAULT_PORTS = {"trips": 8101, "risk": 8102, "cases": 8103}


def require_secret() -> str:
    secret = os.environ.get(SECRET_ENV, "")
    if len(secret.encode()) < 32:
        raise SystemExit(f"{SECRET_ENV} must be set to at least 32 bytes (e.g. `python -c \"import secrets; "
                         f"print(secrets.token_urlsafe(48))\"`)")
    return secret


def build(name: str, db_path: str, secret: str, host: str, port: int) -> Any:
    from driveguard.mcp_servers.cases import build_cases_server
    from driveguard.mcp_servers.risk import build_risk_server
    from driveguard.mcp_servers.trips import build_trips_server

    builders = {"trips": build_trips_server, "risk": build_risk_server, "cases": build_cases_server}
    url = f"http://{host}:{port}/mcp"
    verifier = DriveGuardTokenVerifier(secret, AUD[name])
    return builders[name](db_path, verifier=verifier, resource_url=url)


def http_app(name: str, db_path: str, secret: str, host: str, port: int) -> Any:
    from driveguard.mcp_servers.common import TraceContextMiddleware
    return TraceContextMiddleware(build(name, db_path, secret, host, port).streamable_http_app(host=host))


def serve(name: str, db_path: str, host: str = "127.0.0.1", port: int | None = None) -> None:
    import uvicorn

    secret = require_secret()
    port = port or DEFAULT_PORTS[name]
    uvicorn.run(http_app(name, db_path, secret, host, port), host=host, port=port, log_level="info")
