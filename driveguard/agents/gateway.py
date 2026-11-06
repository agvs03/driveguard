"""Tool gateway: the only path from an agent to data.

Each agent gets its own gateway, opened with its own case-bound token and an
allowlist of `server__tool` names. The gateway:
  * exposes only allowlisted tools to the model (converted to strict schemas);
  * refuses any other tool name the model emits (recorded as a `guard` span);
  * enforces the per-agent / per-case tool budget;
  * records every call (arguments + complete result) in the audit trace;
  * rejects oversized results instead of silently truncating them.

Connectors decide how sessions are opened: in-process (tests, CLI) or over
streamable HTTP with a bearer header (deployed servers).
"""

from __future__ import annotations

import contextlib
import json
import time
from dataclasses import dataclass
from typing import Any, AsyncIterator, Protocol

from mcp import Client

from driveguard.agents.budget import Budget
from driveguard.agents.llm import strict_schema
from driveguard.agents.trace import Span, Trace
from driveguard.obs.context import traceparent
from driveguard.obs.metrics import GATEWAY_LATENCY, GUARD_BLOCKS
from driveguard.obs.tracing import span

SEP = "__"
MAX_RESULT_CHARS = 120_000
AGENT_ACTORS = {"triage", "evidence"}  # LLM-driven actors; guardrails don't apply to code (orchestrator/human)
MAX_EVENT_WINDOW_MS = 10 * 60_000
SPARSE_EVENT_TYPES = frozenset({"phone", "brake", "driver_note", "trip_start", "trip_end"})  # <= ~6 per trip
MAX_DENIALS = 2            # access-control denials (allowlist, token binding): a manipulation signal
MAX_ARGUMENT_REFUSALS = 3  # argument-limit refusals: a usage error, so the agent gets room to correct itself
FOCUS_PAD_MS = 60_000      # suggested window: the incident +/- 1 minute


class Connector(Protocol):
    def session(self, server: str, token: str) -> contextlib.AbstractAsyncContextManager[Client]: ...


class _AuthedSession:
    """Sets the authenticated user around EACH request. The SDK's in-memory streams
    snapshot the *sender's* contextvars on every send and run the handler in that
    snapshot (mcp/shared/_context_streams.py), so identity must be present at call
    time, not at connect time. Per-call scoping also means two open sessions with
    different tokens can never see each other's identity. (DECISIONS M-8.)"""

    def __init__(self, client: Client, user: Any) -> None:
        self._client, self._user = client, user

    @contextlib.contextmanager
    def _as_user(self) -> Any:
        from mcp.server.auth.middleware.auth_context import auth_context_var
        reset = auth_context_var.set(self._user)
        try:
            yield
        finally:
            auth_context_var.reset(reset)

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        with self._as_user():
            return await self._client.call_tool(name, arguments)

    async def list_tools(self) -> Any:
        with self._as_user():
            return await self._client.list_tools()


class InMemoryConnector:
    """Runs the real MCP servers in-process. The token still goes through the
    server's own verifier (signature, audience, expiry, policy) before use."""

    def __init__(self, servers: dict[str, Any], verifiers: dict[str, Any]) -> None:
        self.servers, self.verifiers = servers, verifiers

    @contextlib.asynccontextmanager
    async def session(self, server: str, token: str) -> AsyncIterator[Any]:
        from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser

        access = await self.verifiers[server].verify_token(token)
        user = AuthenticatedUser(access) if access else None
        async with Client(self.servers[server]) as client:
            yield _AuthedSession(client, user)


class HttpConnector:
    def __init__(self, urls: dict[str, str]) -> None:
        self.urls = urls

    @contextlib.asynccontextmanager
    async def session(self, server: str, token: str) -> AsyncIterator[Client]:
        import httpx2
        from mcp.client.streamable_http import streamable_http_client

        headers = {"Authorization": f"Bearer {token}"}
        tp = traceparent()  # session-level: the server's spans parent to the agent span that opened it
        if tp:
            headers["traceparent"] = tp
        async with httpx2.AsyncClient(headers=headers, timeout=30) as http:
            async with Client(streamable_http_client(self.urls[server], http_client=http)) as c:
                yield c


@dataclass
class ToolResult:
    text: str
    is_error: bool
    data: Any = None


class ToolGateway:
    def __init__(self, actor: str, allowed: list[str], sessions: dict[str, Client], tools: dict[str, Any],
                 trace: Trace, budget: Budget | None, guardrails: Any = None,
                 focus: tuple[int, int] | None = None) -> None:
        from driveguard.guardrails import OFF
        self.actor, self.allowed, self.sessions, self._tools = actor, allowed, sessions, tools
        self.trace, self.budget = trace, budget
        self.guardrails = guardrails or OFF
        self.focus = focus  # the incident's (t_start_ms, t_end_ms), used to make refusals actionable
        self.denials = 0
        self.argument_refusals = 0
        self._last_refused: str | None = None

    def _focus_window(self) -> tuple[int, int]:
        """The incident +/- 1 minute, never wider than the argument limit."""
        assert self.focus is not None
        lo, hi = self.focus[0] - FOCUS_PAD_MS, self.focus[1] + FOCUS_PAD_MS
        return (lo, hi) if hi - lo <= MAX_EVENT_WINDOW_MS else (lo, lo + MAX_EVENT_WINDOW_MS)

    def _can_narrow(self, name: str, args: dict[str, Any]) -> bool:
        """A gps/accel listing with no (or half a) window is narrowed to the incident instead of refused.
        Real Claude, at low effort, re-sent the identical refused request up to 3 times even when the refusal
        gave the exact window to use (DECISIONS M-33), so refusing stranded the investigation. Narrowing keeps
        the same bound on what the agent can read, and the trace records it. An explicit window that is too
        wide is still refused: that is a deliberate request for more, not an omission."""
        return bool(self.focus) and name == "trips__list_events" and (
            args.get("t_from_ms") is None or args.get("t_to_ms") is None)

    async def _refused_argument(self, parent: Span | None, name: str, args: dict[str, Any], why: str) -> str:
        """Argument-limit refusals are usage errors, not access violations, so they do NOT feed the
        manipulation abort (real Claude, dev run 2: it asked for 10 gps points with no window, was refused,
        repeated the same call once, and the 2-denial abort escalated 5 of 40 cases; DECISIONS M-33).
        The refusal says exactly what to send instead; a third refusal stops the agent."""
        key = json.dumps([name, args], sort_keys=True)
        if key == self._last_refused:
            why = "this is the same request that was just refused; it will be refused again. " + why
        self._last_refused = key
        if self.focus and name == "trips__list_events":
            lo, hi = self._focus_window()
            why += f". For this incident, retry with \"t_from_ms\": {lo}, \"t_to_ms\": {hi}"
        self.argument_refusals += 1
        if self.guardrails.abort_on_repeated_denials and self.argument_refusals >= MAX_ARGUMENT_REFUSALS:
            from driveguard.agents.specialists import AgentFailure
            GUARD_BLOCKS.inc(guard="argument_abort")
            await self.trace.record("guard", self.actor, {"guardrail": "abort_on_repeated_denials"},
                                    {"argument_refusals": self.argument_refusals, "last": why}, status="error",
                                    parent=parent)
            raise AgentFailure(self.actor, f"stopped after {self.argument_refusals} refused requests "
                                           f"(repeated invalid arguments): {why}")
        return why

    async def _denied(self, parent: Span | None, why: str) -> None:
        """Count a denied call; with the guardrail on, the 2nd denial stops the agent."""
        self.denials += 1
        if self.guardrails.abort_on_repeated_denials and self.actor in AGENT_ACTORS and self.denials >= MAX_DENIALS:
            from driveguard.agents.specialists import AgentFailure
            GUARD_BLOCKS.inc(guard="denial_abort")
            await self.trace.record("guard", self.actor, {"guardrail": "abort_on_repeated_denials"},
                                    {"denials": self.denials, "last": why}, status="error", parent=parent)
            raise AgentFailure(self.actor, f"stopped after {self.denials} denied tool calls (possible manipulation): "
                                           f"{why}")

    def _argument_violation(self, name: str, args: dict[str, Any]) -> str | None:
        if not (self.guardrails.argument_limits and self.actor in AGENT_ACTORS):
            return None
        if name == "trips__list_events":
            types = args.get("types")
            # The limit exists to stop whole-trip dumps of the HIGH-RATE streams (gps/accel: hundreds of events
            # per trip). Sparse types (a handful per trip) may be listed for the whole trip: real Claude asked for
            # a trip's phone events to judge phone distraction, and the old rule (only driver notes exempt) refused
            # it twice and aborted the agent in 3 of 8 phone cases (DECISIONS M-32).
            if isinstance(types, list) and types and set(types) <= SPARSE_EVENT_TYPES:
                return None
            lo, hi = args.get("t_from_ms"), args.get("t_to_ms")
            if lo is None or hi is None:
                return ("list_events over gps/accel (or with no types filter) needs t_from_ms and t_to_ms, at most "
                        f"{MAX_EVENT_WINDOW_MS // 60_000} minutes apart; sparse types "
                        f"({', '.join(sorted(SPARSE_EVENT_TYPES))}) can be listed for the whole trip")
            if hi - lo > MAX_EVENT_WINDOW_MS:
                return f"list_events window is {(hi - lo) // 1000} s; agents are limited to {MAX_EVENT_WINDOW_MS // 1000} s"
        return None

    @classmethod
    @contextlib.asynccontextmanager
    async def open(cls, connector: Connector, token: str, actor: str, allowed: list[str], trace: Trace,
                   budget: Budget | None = None, guardrails: Any = None,
                   focus: tuple[int, int] | None = None) -> AsyncIterator["ToolGateway"]:
        servers = sorted({n.split(SEP, 1)[0] for n in allowed})
        async with contextlib.AsyncExitStack() as stack:
            sessions = {s: await stack.enter_async_context(connector.session(s, token)) for s in servers}
            tools: dict[str, Any] = {}
            for s, c in sessions.items():
                for t in (await c.list_tools()).tools:
                    name = f"{s}{SEP}{t.name}"
                    if name in allowed:
                        tools[name] = t
            missing = set(allowed) - set(tools)
            if missing:
                raise RuntimeError(f"allowlisted tools not offered by servers: {sorted(missing)}")
            yield cls(actor, allowed, sessions, tools, trace, budget, guardrails, focus)

    def claude_tools(self) -> list[dict[str, Any]]:
        """Deterministic order (sorted) so the tool block is a stable cache prefix."""
        return [{"name": n, "description": (self._tools[n].description or "")[:1000],
                 "input_schema": strict_schema(self._tools[n].input_schema)}
                for n in sorted(self._tools)]

    async def call(self, name: str, arguments: dict[str, Any], *, parent: Span | None = None,
                   count_budget: bool = True) -> ToolResult:
        if name not in self._tools:
            GUARD_BLOCKS.inc(guard="tool_allowlist")
            msg = {"code": "FORBIDDEN", "message": f"tool {name!r} is not available to {self.actor}",
                   "retryable": False}
            await self.trace.record("guard", self.actor, {"tool": name, "arguments": arguments}, msg,
                                    status="error", parent=parent)
            await self._denied(parent, f"tool {name} not allowed")
            return ToolResult(json.dumps(msg), True)
        violation = self._argument_violation(name, arguments)
        note = None
        if violation and self._can_narrow(name, arguments):
            lo, hi = self._focus_window()
            GUARD_BLOCKS.inc(guard="argument_narrowed")
            await self.trace.record("guard", self.actor, {"tool": name, "arguments": arguments,
                                                          "guardrail": "argument_limits", "action": "narrowed"},
                                    {"narrowed_to": {"t_from_ms": lo, "t_to_ms": hi}, "reason": violation},
                                    parent=parent)
            arguments = {**arguments, "t_from_ms": lo, "t_to_ms": hi}
            note = (f"gps/accel listings need a time window, so the gateway applied this incident's window "
                    f"(t_from_ms={lo}, t_to_ms={hi}). Pass t_from_ms/t_to_ms (at most 10 minutes apart) "
                    f"to choose a different one.")
            violation = None
        if violation:
            GUARD_BLOCKS.inc(guard="argument_limits")
            msg = {"code": "INVALID_ARGUMENT", "message": violation, "retryable": True}
            await self.trace.record("guard", self.actor, {"tool": name, "arguments": arguments,
                                                          "guardrail": "argument_limits"}, msg,
                                    status="error", parent=parent)
            msg["message"] = await self._refused_argument(parent, name, arguments, violation)
            return ToolResult(json.dumps(msg), True)
        if count_budget and self.budget is not None:
            self.budget.before_tool_call(self.actor)
        server, tool = name.split(SEP, 1)
        t0 = time.perf_counter()
        with span("tool_call", tool=name, actor=self.actor) as sp:
            r = await self.sessions[server].call_tool(tool, arguments)
            if r.is_error:
                sp.error("tool returned an error")
        latency = (time.perf_counter() - t0) * 1000
        GATEWAY_LATENCY.observe(latency / 1000, tool=name)
        if r.is_error:
            text = r.content[0].text if r.content else '{"code":"INTERNAL","message":"empty error"}'
            try:
                out: Any = json.loads(text)
            except ValueError:
                out = {"code": "INTERNAL", "message": text[:300]}
            await self.trace.record("tool_call", self.actor, {"tool": name, "arguments": arguments}, out,
                                    status="error", parent=parent, latency_ms=latency)
            if isinstance(out, dict) and out.get("code") == "FORBIDDEN":
                await self._denied(parent, f"{name}: {out.get('message', '')}")
            return ToolResult(json.dumps(out), True, out)
        data = r.structured_content
        if isinstance(data, dict) and set(data) == {"result"}:
            data = data["result"]
        if note and isinstance(data, dict):
            data = {**data, "gateway_note": note}
        text = json.dumps(data, separators=(",", ":"))
        if len(text) > MAX_RESULT_CHARS:
            err = {"code": "RESULT_TOO_LARGE", "message": f"result is {len(text)} chars; narrow the query "
                   f"(time range, types, limit) to stay under {MAX_RESULT_CHARS}", "retryable": False}
            await self.trace.record("tool_call", self.actor, {"tool": name, "arguments": arguments}, err,
                                    status="error", parent=parent, latency_ms=latency)
            return ToolResult(json.dumps(err), True, err)
        await self.trace.record("tool_call", self.actor, {"tool": name, "arguments": arguments}, data,
                                parent=parent, latency_ms=latency)
        return ToolResult(text, False, data)
