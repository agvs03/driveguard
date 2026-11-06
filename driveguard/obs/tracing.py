"""Minimal distributed tracing: timed spans with W3C-compatible ids.

    with span("agent", agent="triage") as s:
        ...
        s.set(model_calls=3)

A span inherits trace_id from the current context (or starts a trace), makes
itself the current span for anything nested (including in-process MCP tool
handlers, and HTTP calls, which forward `traceparent`), and on exit hands a
finished record to every registered exporter:

    {"trace_id", "span_id", "parent_span_id", "name", "start_ms",
     "duration_ms", "status", "error", "attrs"}

Exporters: MemoryExporter (tests, the SLO bench), JsonlExporter (a file any
trace tool can ingest). Spans are also logged at DEBUG. Attributes are for
small closed-set values and numbers; payloads never go in a span.
"""

from __future__ import annotations

import contextlib
import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterator

from driveguard.obs.context import _CTX, new_span_id, new_trace_id
from driveguard.obs.logging import log_event

log = logging.getLogger("driveguard.trace")
Exporter = Callable[[dict[str, Any]], None]
_EXPORTERS: list[Exporter] = []
_MAX_ATTR = 200


class SpanHandle:
    def __init__(self, record: dict[str, Any]) -> None:
        self.record = record

    @property
    def trace_id(self) -> str:
        return self.record["trace_id"]

    @property
    def span_id(self) -> str:
        return self.record["span_id"]

    def set(self, **attrs: Any) -> None:
        for k, v in attrs.items():
            if isinstance(v, str) and len(v) > _MAX_ATTR:
                v = v[:_MAX_ATTR] + "..."
            self.record["attrs"][k] = v

    def error(self, message: str) -> None:
        self.record["status"] = "error"
        self.record["error"] = message[:_MAX_ATTR]


@contextlib.contextmanager
def span(name: str, *, trace_id: str | None = None, parent_span_id: str | None = None,
         **attrs: Any) -> Iterator[SpanHandle]:
    """`trace_id`/`parent_span_id` override the context: used to start a run's trace
    (trace_id = run_id) and to continue a trace received over HTTP."""
    ctx = _CTX.get()
    tid = trace_id or ctx.get("trace_id") or new_trace_id()
    parent = parent_span_id if parent_span_id is not None else (ctx.get("span_id") if tid == ctx.get("trace_id")
                                                                else None)
    rec: dict[str, Any] = {"trace_id": tid, "span_id": new_span_id(), "parent_span_id": parent, "name": name,
                           "start_ms": int(time.time() * 1000), "duration_ms": 0.0, "status": "ok", "error": None,
                           "attrs": {}}
    h = SpanHandle(rec)
    h.set(**attrs)
    token = _CTX.set({**ctx, "trace_id": tid, "span_id": rec["span_id"]})
    t0 = time.perf_counter()
    try:
        yield h
    except BaseException as e:
        if rec["status"] == "ok":
            h.error(f"{type(e).__name__}: {e}")
        raise
    finally:
        rec["duration_ms"] = round((time.perf_counter() - t0) * 1000, 3)
        _CTX.reset(token)
        _export(rec)


def _export(rec: dict[str, Any]) -> None:
    for ex in list(_EXPORTERS):
        try:
            ex(rec)
        except Exception:  # noqa: BLE001 - telemetry must never break the work it observes
            log.exception("span exporter failed")
    if log.isEnabledFor(logging.DEBUG):
        log_event(log, "span", logging.DEBUG, span_name=rec["name"], duration_ms=rec["duration_ms"],
                  status=rec["status"], parent_span_id=rec["parent_span_id"], **rec["attrs"])


def add_exporter(ex: Exporter) -> Exporter:
    _EXPORTERS.append(ex)
    return ex


def remove_exporter(ex: Exporter) -> None:
    with contextlib.suppress(ValueError):
        _EXPORTERS.remove(ex)


class MemoryExporter:
    def __init__(self, max_spans: int = 100_000) -> None:
        self.spans: list[dict[str, Any]] = []
        self._max = max_spans
        self._lock = threading.Lock()

    def __call__(self, rec: dict[str, Any]) -> None:
        with self._lock:
            self.spans.append(rec)
            del self.spans[:-self._max]

    def by_trace(self, trace_id: str) -> list[dict[str, Any]]:
        return [s for s in self.spans if s["trace_id"] == trace_id]


class JsonlExporter:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def __call__(self, rec: dict[str, Any]) -> None:
        line = json.dumps(rec, default=str)
        with self._lock, open(self.path, "a", encoding="utf-8") as f:
            f.write(line + "\n")


@contextlib.contextmanager
def capture() -> Iterator[MemoryExporter]:
    ex = MemoryExporter()
    add_exporter(ex)
    try:
        yield ex
    finally:
        remove_exporter(ex)
