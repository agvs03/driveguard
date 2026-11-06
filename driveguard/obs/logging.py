"""Structured logging: one JSON object per line.

    {"ts": "2026-11-05T18:02:11.412Z", "level": "info", "logger": "driveguard.mcp",
     "event": "mcp_tool_call", "trace_id": "...", "run_id": "...", "server": "trips",
     "tool": "list_events", "code": "OK", "latency_ms": 3.1}

Rules:
  * `event` is a stable snake_case name; values go in fields, never formatted
    into the message, so logs can be queried without regexes;
  * context (trace/run/case ids) is added from contextvars, not passed by hand;
  * every string is passed through credential redaction on the way out;
  * payloads (event data, note text, prompts) are never logged: they live in
    the audit trace, which has access control. Logs have none.
"""

from __future__ import annotations

import json
import logging
import sys
import time
from typing import IO, Any

from driveguard.obs.context import current
from driveguard.obs.redact import redact, redact_str

_RESERVED = set(vars(logging.makeLogRecord({}))) | {"message", "asctime", "fields"}
_MAX_FIELD_CHARS = 2000


def _clip(v: Any) -> Any:
    if isinstance(v, str) and len(v) > _MAX_FIELD_CHARS:
        return v[:_MAX_FIELD_CHARS] + f"...[{len(v) - _MAX_FIELD_CHARS} more chars]"
    return v


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        ms = int(record.created * 1000) % 1000
        out: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)) + f".{ms:03d}Z",
            "level": record.levelname.lower(),
            "logger": record.name,
            "event": redact_str(record.getMessage()),
        }
        out.update(current())
        fields = getattr(record, "fields", None) or {}
        # Plain `extra={...}` keys are accepted too, so stdlib-style calls still produce fields.
        fields = {**{k: v for k, v in vars(record).items() if k not in _RESERVED}, **fields}
        for k, v in fields.items():
            out.setdefault(k, _clip(redact(v)))
        if record.exc_info:
            out["exc"] = redact_str(self.formatException(record.exc_info))[-4000:]
        return json.dumps(out, default=str, ensure_ascii=False)


class TextFormatter(logging.Formatter):
    """Human-readable variant for a terminal: `level logger event k=v ...`."""

    def format(self, record: logging.LogRecord) -> str:
        fields = {**current(), **(getattr(record, "fields", None) or {})}
        kv = " ".join(f"{k}={_clip(redact(v))}" for k, v in fields.items() if k not in ("trace_id", "span_id"))
        line = f"{record.levelname.lower():<7} {record.name:<24} {redact_str(record.getMessage())} {kv}".rstrip()
        if record.exc_info:
            line += "\n" + redact_str(self.formatException(record.exc_info))
        return line


def configure_logging(level: str | int = "INFO", fmt: str = "json", stream: IO[str] | None = None) -> logging.Handler:
    """Install (or replace) DriveGuard's handler on the `driveguard` logger. Idempotent."""
    root = logging.getLogger("driveguard")
    for h in list(root.handlers):
        if getattr(h, "_driveguard", False):
            root.removeHandler(h)
    handler = logging.StreamHandler(stream or sys.stderr)
    handler.setFormatter(JsonFormatter() if fmt == "json" else TextFormatter())
    handler._driveguard = True  # type: ignore[attr-defined]
    root.addHandler(handler)
    root.setLevel(level if isinstance(level, int) else level.upper())
    root.propagate = False
    return handler


def log_event(logger: logging.Logger, event: str, level: int = logging.INFO, **fields: Any) -> None:
    if logger.isEnabledFor(level):
        logger.log(level, event, extra={"fields": fields})
