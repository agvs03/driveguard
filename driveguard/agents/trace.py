"""Append-only, hash-chained audit trace.

One chain per investigation run. Each span's hash covers the previous hash
and a canonical JSON of the span, so editing, deleting or reordering any span
breaks `verify_run`. The table also has triggers that refuse UPDATE/DELETE.

What gets recorded:
  model_call   full request summary (agent, system prompt hash, the messages
               added since the last call), response blocks, usage, cost
  tool_call    tool name, arguments, the complete result (this is what makes
               citations checkable: a cited event must appear here)
  state/guard/validation/human_decision/run_start/run_end

Secrets are redacted before hashing: anything shaped like a JWT or an
Anthropic key never reaches the table.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable

from driveguard.obs.redact import redact  # noqa: F401  (re-exported; shared with the log formatter)
from driveguard.store.db import Database, transaction


def canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str, ensure_ascii=False)


def genesis(run_id: str) -> str:
    return hashlib.sha256(f"driveguard-trace-genesis:{run_id}".encode()).hexdigest()


_HASHED = ("run_id", "seq", "case_id", "span_id", "parent_span_id", "kind", "actor", "status", "input_json",
           "output_json", "model", "usage_json", "cost_usd", "latency_ms", "at_ms")


def span_hash(prev_hash: str, row: dict[str, Any]) -> str:
    body = canonical({k: row[k] for k in _HASHED})
    return hashlib.sha256((prev_hash + body).encode()).hexdigest()


@dataclass(frozen=True)
class Span:
    span_id: str
    seq: int


class Trace:
    """Writer for one run's chain. Safe for concurrent async callers."""

    def __init__(self, db: Database, run_id: str, case_id: str | None = None,
                 now_ms: Callable[[], int] = lambda: int(time.time() * 1000)) -> None:
        self.db, self.run_id, self.case_id, self.now_ms = db, run_id, case_id, now_ms
        self._lock = asyncio.Lock()

    async def record(self, kind: str, actor: str, input: Any, output: Any, *, status: str = "ok",
                     parent: Span | None = None, model: str | None = None, usage: dict[str, Any] | None = None,
                     cost_usd: float = 0.0, latency_ms: float = 0.0) -> Span:
        async with self._lock:
            span_id = str(uuid.uuid4())

            def _write(conn: sqlite3.Connection) -> int:
                with transaction(conn):
                    last = conn.execute("SELECT seq, hash FROM audit_trace WHERE run_id = ? ORDER BY seq DESC LIMIT 1",
                                        (self.run_id,)).fetchone()
                    seq = (last["seq"] + 1) if last else 0
                    prev = last["hash"] if last else genesis(self.run_id)
                    row = {
                        "run_id": self.run_id, "seq": seq, "case_id": self.case_id, "span_id": span_id,
                        "parent_span_id": parent.span_id if parent else None, "kind": kind, "actor": actor,
                        "status": status, "input_json": canonical(redact(input)),
                        "output_json": canonical(redact(output)), "model": model,
                        "usage_json": canonical(usage) if usage is not None else None,
                        "cost_usd": round(float(cost_usd), 8), "latency_ms": round(float(latency_ms), 3),
                        "at_ms": self.now_ms(),
                    }
                    row["prev_hash"] = prev
                    row["hash"] = span_hash(prev, row)
                    cols = list(row)
                    conn.execute(f"INSERT INTO audit_trace ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                                 [row[c] for c in cols])
                    return seq

            seq = await self.db.run(_write)
            return Span(span_id, seq)


def _rows(conn: sqlite3.Connection, sql: str, args: tuple[Any, ...]) -> list[sqlite3.Row]:
    cur = conn.cursor()
    cur.row_factory = sqlite3.Row  # independent of the caller's connection settings
    return cur.execute(sql, args).fetchall()


def verify_run(conn: sqlite3.Connection, run_id: str) -> tuple[bool, int | None]:
    """(ok, first_bad_seq). Detects edited, removed, inserted or reordered spans."""
    prev = genesis(run_id)
    expected_seq = 0
    for r in _rows(conn, "SELECT * FROM audit_trace WHERE run_id = ? ORDER BY seq", (run_id,)):
        row = dict(r)
        if row["seq"] != expected_seq or row["prev_hash"] != prev or span_hash(prev, row) != row["hash"]:
            return False, row["seq"]
        prev = row["hash"]
        expected_seq += 1
    return True, None


def load_spans(conn: sqlite3.Connection, run_id: str) -> list[dict[str, Any]]:
    out = []
    for r in _rows(conn, "SELECT * FROM audit_trace WHERE run_id = ? ORDER BY seq", (run_id,)):
        d = dict(r)
        d["input"] = json.loads(d.pop("input_json"))
        d["output"] = json.loads(d.pop("output_json"))
        raw_usage = d.pop("usage_json")
        d["usage"] = json.loads(raw_usage) if raw_usage else None
        out.append(d)
    return out


def events_seen(spans: list[dict[str, Any]]) -> dict[str, tuple[dict[str, Any], str]]:
    """event_id -> (full event record, span_id of the tool call that returned it).

    Only *full event records* count (objects with event_id + type + payload), not
    ids merely listed somewhere (e.g. an incident's trigger_event_ids). A claim
    can only be grounded in data a tool actually returned.
    """
    seen: dict[str, tuple[dict[str, Any], str]] = {}

    def walk(x: Any, span_id: str) -> None:
        if isinstance(x, dict):
            if isinstance(x.get("event_id"), str) and "payload" in x and "type" in x:
                seen.setdefault(x["event_id"], (x, span_id))
            for v in x.values():
                walk(v, span_id)
        elif isinstance(x, list):
            for v in x:
                walk(v, span_id)

    for s in spans:
        if s["kind"] == "tool_call" and s["status"] == "ok":
            walk(s["output"], s["span_id"])
    return seen
