"""LLM clients behind one interface.

* ClaudeClient   - the official `anthropic` SDK (async), Messages API with
                   strict tools + structured output for the final answer,
                   adaptive thinking with an explicit effort, automatic prompt
                   caching, and server-side refusal fallbacks.
* ScriptedClient - deterministic, free, offline (see scripted.py).

We own the agent loop (rather than the SDK's beta tool runner) because every
model call must pass a budget check *before* it is sent and be written to the
hash-chained trace with its exact usage. See DECISIONS D-23.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from typing import Any, Protocol

from driveguard.agents.budget import Usage

DEFAULT_MODEL = "claude-opus-5-5"


class LLMError(Exception):
    """Model call failed (API error, refusal, truncation). Not retried by the agent loop."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(f"{kind}: {message}")
        self.kind = kind


@dataclass
class ToolCall:
    id: str
    name: str
    input: dict[str, Any]


@dataclass
class LLMResponse:
    model: str
    stop_reason: str
    text: str
    tool_calls: list[ToolCall]
    raw_content: list[Any]  # echo back verbatim (thinking blocks must be passed back unchanged)
    usage: Usage
    trace_content: list[dict[str, Any]] = field(default_factory=list)  # JSON-safe copy for the audit trace


class LLMClient(Protocol):
    name: str
    model: str

    async def create(self, *, agent: str, system: str, messages: list[dict[str, Any]],
                     tools: list[dict[str, Any]], output_schema: dict[str, Any], effort: str,
                     max_tokens: int) -> LLMResponse: ...


# ------------------------------------------------------------------ schemas --- #

_DROP = {"minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf", "minLength", "maxLength",
         "pattern", "minItems", "maxItems", "uniqueItems", "title", "default", "examples"}
_FORMATS = {"date-time", "time", "date", "duration", "email", "hostname", "uri", "ipv4", "ipv6", "uuid"}


def strict_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Make a JSON Schema acceptable to strict tool use / structured outputs:
    drop unsupported constraints (they are still enforced server-side by the MCP
    servers, and client-side by pydantic for our own outputs) and close every object."""

    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            out = {}
            for k, v in node.items():
                if k in _DROP:
                    continue
                if k == "format" and v not in _FORMATS:
                    continue
                if k in ("properties", "$defs", "definitions"):
                    out[k] = {pk: walk(pv) for pk, pv in v.items()}
                else:
                    out[k] = walk(v)
            if out.get("type") == "object" or "properties" in out:
                out["additionalProperties"] = False
                out.setdefault("properties", {})
            return out
        if isinstance(node, list):
            return [walk(x) for x in node]
        return node

    return walk(copy.deepcopy(schema))


# Measured, not assumed (DECISIONS M-25): on 127 real Opus 5.5 calls the actual input was 1.36-1.84x
# chars/3 (median 1.55x), i.e. ~1.6-2.2 chars per token. Our prompts are JSON full of UUIDs and numbers,
# which tokenize far finer than prose. 1.4 chars/token keeps the worst observed call ~14% under.
CHARS_PER_TOKEN = 1.4


def estimate_tokens(obj: Any) -> int:
    """Cheap, conservative estimate used only for the pre-call budget check. The agent loop
    records a `guard` span whenever the real count exceeds it, so drift stays visible."""
    return int(len(json.dumps(obj, default=str)) / CHARS_PER_TOKEN) + 50


# ----------------------------------------------------------------- Claude --- #

class ClaudeClient:
    name = "claude"

    def __init__(self, model: str = DEFAULT_MODEL, *, client: Any = None, fallbacks: bool = True,
                 timeout_s: float = 120.0) -> None:
        import anthropic

        import os

        self.model = model
        self.fallbacks = fallbacks
        # Credentials resolve from the environment (ANTHROPIC_API_KEY, or an `ant auth login` profile).
        # A key that is not scoped to a workspace needs the anthropic-workspace-id header on every
        # request; the core client sends it only via default_headers (found in the first live smoke run).
        headers = {}
        if os.environ.get("ANTHROPIC_WORKSPACE_ID"):
            headers["anthropic-workspace-id"] = os.environ["ANTHROPIC_WORKSPACE_ID"]
        self._client = client or anthropic.AsyncAnthropic(timeout=timeout_s, max_retries=2,
                                                          default_headers=headers or None)
        self._anthropic = anthropic

    async def aclose(self) -> None:
        await self._client.close()

    def build_request(self, *, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]],
                      output_schema: dict[str, Any], effort: str, max_tokens: int) -> dict[str, Any]:
        req: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": messages,
            "thinking": {"type": "adaptive"},
            "output_config": {"effort": effort,
                              "format": {"type": "json_schema", "schema": strict_schema(output_schema)}},
            "cache_control": {"type": "ephemeral"},  # automatic caching of the growing prefix
        }
        if tools:
            req["tools"] = [{**t, "strict": True} for t in tools]
        if self.fallbacks:
            req["betas"] = ["server-side-fallback-2026-07-01"]
            req["fallbacks"] = "default"
        return req

    async def create(self, *, agent: str, system: str, messages: list[dict[str, Any]],
                     tools: list[dict[str, Any]], output_schema: dict[str, Any], effort: str,
                     max_tokens: int) -> LLMResponse:
        a = self._anthropic
        req = self.build_request(system=system, messages=messages, tools=tools, output_schema=output_schema,
                                 effort=effort, max_tokens=max_tokens)
        try:
            if self.fallbacks:
                resp = await self._client.beta.messages.create(**req)
            else:
                resp = await self._client.messages.create(**req)
        except a.RateLimitError as e:
            raise LLMError("rate_limited", str(e)[:300]) from None
        except a.BadRequestError as e:
            raise LLMError("bad_request", str(e)[:300]) from None
        except (a.AuthenticationError, a.PermissionDeniedError) as e:
            raise LLMError("auth", str(e)[:200]) from None
        except a.APIStatusError as e:
            raise LLMError(f"api_{e.status_code}", str(e)[:300]) from None
        except a.APIConnectionError as e:
            raise LLMError("connection", str(e)[:200]) from None

        if resp.stop_reason == "refusal":
            cat = getattr(getattr(resp, "stop_details", None), "category", None)
            raise LLMError("refusal", f"model declined (category={cat})")
        if resp.stop_reason == "max_tokens":
            raise LLMError("truncated", f"hit max_tokens={max_tokens}")
        u = resp.usage
        usage = Usage(input_tokens=u.input_tokens or 0, output_tokens=u.output_tokens or 0,
                      cache_read_input_tokens=getattr(u, "cache_read_input_tokens", 0) or 0,
                      cache_creation_input_tokens=getattr(u, "cache_creation_input_tokens", 0) or 0)
        calls = [ToolCall(b.id, b.name, dict(b.input)) for b in resp.content if b.type == "tool_use"]
        text = "".join(b.text for b in resp.content if b.type == "text")
        trace = [b.model_dump(exclude_none=True) for b in resp.content]
        for b in trace:  # thinking signatures are opaque blobs; keep the trace readable
            if b.get("type") == "thinking" and "signature" in b:
                b["signature"] = f"<{len(b['signature'])} bytes>"
        return LLMResponse(model=resp.model, stop_reason=resp.stop_reason, text=text, tool_calls=calls,
                           raw_content=list(resp.content), usage=usage, trace_content=trace)
