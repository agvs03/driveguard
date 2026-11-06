"""Shared plumbing for the three MCP servers: strict argument schemas, auth,
rate limiting, a uniform error envelope and per-call logging.

Every tool error a client sees is a JSON object
    {"code": "...", "message": "...", "retryable": bool}
whether it came from our code (DriveGuardError), from the SDK's argument
validation, or from an unexpected crash (which becomes a generic INTERNAL with
no detail; the traceback stays in the server log).
"""

import contextlib
import functools
import json
import logging
import re
import threading
import time
from collections import defaultdict, deque
from typing import Any, AsyncIterator, Awaitable, Callable

from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.mcpserver.utilities import func_metadata as _fm
from pydantic import ConfigDict

from driveguard.errors import Code, DriveGuardError
from driveguard.mcp_servers.auth import Principal, current_principal
from driveguard.obs.context import bind, parse_traceparent
from driveguard.obs.logging import log_event
from driveguard.obs.metrics import MCP_CALLS, MCP_LATENCY
from driveguard.obs.tracing import span
from driveguard.store.db import Database

log = logging.getLogger("driveguard.mcp")

# --------------------------------------------------------------------------- #
# Strict arguments. The SDK builds every tool's argument model on ArgModelBase,
# which ignores unknown keys by default: a typo like `type=` for `types=` would
# silently return unfiltered data. Forbidding extras also makes the advertised
# JSON Schema carry `additionalProperties: false`, which Claude's strict tool
# use requires. This touches an SDK module attribute, so tests assert it holds.
# --------------------------------------------------------------------------- #
_fm.ArgModelBase.model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")

UUID_RE = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"


def _envelope(code: Code, message: str) -> str:
    return json.dumps(DriveGuardError(code, message).envelope())


_PREFIX = re.compile(r"^Error executing tool [\w.-]+(: )?")
_PYDANTIC_NOISE = re.compile(r"\s*For further information visit \S+")
_INPUT_VALUE = re.compile(r", input_value=.*?, input_type=\w+\]")


def normalize_error_text(text: str) -> str:
    """Turn any SDK/tool error text into our JSON envelope (idempotent)."""
    body = _PREFIX.sub("", text, count=1).strip()
    with contextlib.suppress(ValueError):
        obj = json.loads(body)
        if isinstance(obj, dict) and "code" in obj:
            return json.dumps(obj)
    if not body:  # bare "Error executing tool X": an unexpected crash, detail withheld
        return _envelope(Code.INTERNAL, "internal error")
    if "validation error" in body:
        clean = _INPUT_VALUE.sub("]", _PYDANTIC_NOISE.sub("", body))
        return _envelope(Code.INVALID_ARGUMENT, clean[:600])
    if body.lower().startswith("unknown tool"):
        return _envelope(Code.NOT_FOUND, body[:200])
    return _envelope(Code.INVALID_ARGUMENT, body[:300])


async def envelope_middleware(ctx: Any, call_next: Callable[[Any], Awaitable[Any]]) -> Any:
    result = await call_next(ctx)
    if getattr(ctx, "method", None) == "tools/call" and isinstance(result, dict) and result.get("isError"):
        for block in result.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "text":
                block["text"] = normalize_error_text(block.get("text", ""))
        result.pop("structuredContent", None)
    return result


class RateLimiter:
    """Sliding-window limiter per principal (in-memory, per server process)."""

    def __init__(self, max_calls: int, per_s: float, now: Callable[[], float] = time.monotonic) -> None:
        self.max_calls, self.per_s, self._now = max_calls, per_s, now
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def check(self, key: str) -> None:
        with self._lock:
            now = self._now()
            q = self._hits[key]
            while q and q[0] <= now - self.per_s:
                q.popleft()
            if len(q) >= self.max_calls:
                raise DriveGuardError(Code.RATE_LIMITED, f"rate limit of {self.max_calls} calls per "
                                                         f"{self.per_s:g}s exceeded; retry later")
            q.append(now)


class ToolKit:
    """Per-server state: DB handle, clock, limiter, and the `guard` decorator."""

    def __init__(self, server_name: str, db_path: str, *, read_only: bool, limiter: RateLimiter,
                 now_ms: Callable[[], int] = lambda: int(time.time() * 1000)) -> None:
        self.server_name = server_name
        self.db = Database(db_path, read_only=read_only)
        self.limiter = limiter
        self.now_ms = now_ms
        self.calls: list[dict[str, Any]] = []  # recent call log (tests/ops); bounded below

    async def run(self, fn: Callable[..., Any], *args: Any) -> Any:
        return await self.db.run(fn, *args)

    def guard(self, scope: str) -> Callable[[Callable[..., Awaitable[Any]]], Callable[..., Awaitable[Any]]]:
        """Authenticate, check scope, rate-limit, map errors, and log every call."""

        def deco(fn: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
            @functools.wraps(fn)
            async def wrapper(*args: Any, **kwargs: Any) -> Any:
                t0 = time.perf_counter()
                p: Principal | None = None
                code = "OK"
                with bind(component=f"mcp:{self.server_name}"),                         span("mcp_tool", server=self.server_name, tool=fn.__name__) as sp:
                    try:
                        p = current_principal()
                        p.require_scope(scope)
                        self.limiter.check(p.sub)
                        return await fn(*args, **kwargs)
                    except DriveGuardError as e:
                        code = e.code.value
                        raise ToolError(json.dumps(e.envelope())) from None
                    except ToolError:
                        code = "TOOL_ERROR"
                        raise
                    except Exception:
                        code = "INTERNAL"
                        log.exception("tool %s.%s crashed", self.server_name, fn.__name__)
                        raise ToolError(_envelope(Code.INTERNAL, "internal error")) from None
                    finally:
                        latency_s = time.perf_counter() - t0
                        rec = {"server": self.server_name, "tool": fn.__name__, "code": code,
                               "sub": p.sub if p else None, "role": p.role if p else None,
                               "case_id": p.case_id if p else None, "latency_ms": round(latency_s * 1000, 2)}
                        self.calls.append(rec)
                        del self.calls[:-1000]
                        MCP_CALLS.inc(server=self.server_name, tool=fn.__name__, code=code)
                        MCP_LATENCY.observe(latency_s, server=self.server_name, tool=fn.__name__)
                        sp.set(code=code, role=rec["role"])
                        if code != "OK":
                            sp.error(code)
                        log_event(log, "mcp_tool_call", **rec)

            return wrapper

        return deco


class TraceContextMiddleware:
    """ASGI middleware: continue the caller's trace from a W3C `traceparent` header.
    The SDK runs tool handlers in the request's context (that is also how the auth
    contextvar reaches them), so binding here is enough."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers") or [])
        parsed = parse_traceparent(headers.get(b"traceparent", b"").decode("latin-1"))
        if parsed is None:
            return await self.app(scope, receive, send)
        trace_id, parent = parsed
        with bind(trace_id=trace_id, span_id=parent):
            return await self.app(scope, receive, send)


def make_server(name: str, instructions: str, kit: ToolKit, *, token_verifier: Any = None,
                resource_url: str | None = None) -> MCPServer:
    @contextlib.asynccontextmanager
    async def lifespan(_: MCPServer) -> AsyncIterator[None]:
        await kit.db.connect()
        try:
            yield None
        finally:
            await kit.db.close()

    auth = None
    if token_verifier is not None:
        url = resource_url or "http://127.0.0.1/mcp"
        # validate_token_resource=False: our verifier already enforces the JWT `aud`
        # (a logical name like "driveguard:trips", not the URL), so the SDK's URL
        # comparison would wrongly refuse every token. Covered by test_mcp_http.
        auth = AuthSettings(issuer_url=url.rsplit("/mcp", 1)[0] or url, resource_server_url=url,
                            validate_token_resource=False)
    return MCPServer(name, instructions=instructions, token_verifier=token_verifier, auth=auth,
                     lifespan=lifespan, middleware=[envelope_middleware])
