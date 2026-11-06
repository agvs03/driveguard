"""Request-scoped context (trace ids, run/case ids) carried in contextvars.

contextvars follow asyncio tasks automatically, and the MCP SDK's in-memory
transport runs each handler in the *sender's* context snapshot (DECISIONS
M-8), so in-process tool calls inherit the caller's trace without any
plumbing. Over HTTP the context crosses as a W3C `traceparent` header.
"""

from __future__ import annotations

import contextlib
import contextvars
import re
import secrets
from typing import Any, Iterator

_CTX: contextvars.ContextVar[dict[str, Any]] = contextvars.ContextVar("driveguard_obs", default={})

# Fields allowed into log lines from context. Anything else passed to bind() is ignored,
# so a careless bind(note=text) can never put payload text into every log line.
FIELDS = ("trace_id", "span_id", "run_id", "case_id", "incident_id", "agent", "component", "request_id")


def current() -> dict[str, Any]:
    return dict(_CTX.get())


@contextlib.contextmanager
def bind(**fields: Any) -> Iterator[dict[str, Any]]:
    new = {**_CTX.get(), **{k: v for k, v in fields.items() if k in FIELDS and v is not None}}
    token = _CTX.set(new)
    try:
        yield new
    finally:
        _CTX.reset(token)


def annotate(**fields: Any) -> None:
    """Add fields to the current context without opening a new scope. Only use inside a
    bind()/span() block: that block's exit restores the earlier value, so nothing leaks."""
    _CTX.set({**_CTX.get(), **{k: v for k, v in fields.items() if k in FIELDS and v is not None}})


def new_trace_id() -> str:
    return secrets.token_hex(16)


def new_span_id() -> str:
    return secrets.token_hex(8)


_TRACEPARENT = re.compile(r"^00-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})$")


def traceparent() -> str | None:
    c = _CTX.get()
    if not c.get("trace_id") or not c.get("span_id"):
        return None
    return f"00-{c['trace_id']}-{c['span_id']}-01"


def parse_traceparent(value: str | None) -> tuple[str, str] | None:
    """(trace_id, parent_span_id) or None. All-zero ids are invalid per the W3C spec."""
    m = _TRACEPARENT.match((value or "").strip().lower())
    if not m or set(m.group(1)) == {"0"} or set(m.group(2)) == {"0"}:
        return None
    return m.group(1), m.group(2)
