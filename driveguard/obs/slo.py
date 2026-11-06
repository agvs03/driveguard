"""Service level objectives: definitions, evaluation and error budgets.

Every SLO is a ratio of good events to valid events over a window (the SRE
book's form), never an average: "95% of investigations finish within 90 s",
not "mean latency 40 s". The error budget is the allowed bad fraction,
1 - objective; `budget_used` > 1.0 means the SLO is blown.

Where the data comes from:
  db       durable records: the audit trace (client-observed tool latency and
           errors), investigation_runs, the broker's queue table. Works on any
           DriveGuard database, including the real-Claude eval runs.
  metrics  in-process histograms: ingest latency and API latency only exist
           while a process runs, so these are measured by `driveguard slo
           --bench` (or scraped from /metrics in a deployment).

What is deliberately NOT counted against availability: producer errors
(schema-invalid or conflicting events go to the DLQ by design), FORBIDDEN or
INVALID_ARGUMENT tool results (the caller's mistake or a guardrail working),
and escalations (a correct, safe outcome: a human decides).
"""

from __future__ import annotations

import json
import math
import sqlite3
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

from driveguard.obs import metrics as m

SERVER_FAULT_CODES = ("INTERNAL", "UNAVAILABLE")
STUCK_RUN_MS = 10 * 60_000  # a run still 'running' after this is counted as failed


@dataclass(frozen=True)
class SLO:
    name: str
    sli: str            # what is measured, in words
    objective: float    # e.g. 0.95
    source: str         # "db" | "queue" | "metrics"
    threshold: float | None = None  # latency/cost bound for ratio-of-fast SLIs
    unit: str = ""
    why: str = ""


SLOS: list[SLO] = [
    SLO("ingest-latency", "events durable (stored, or recognised as duplicate) within 2 s of publish", 0.99,
        "metrics", 2.0, "s", "the detector and sweeper assume near-real-time data; 2 s at 500 events/s locally"),
    SLO("ingest-availability", "valid messages stored without exhausting retries (producer errors excluded)",
        0.999, "queue", why="a dead-lettered valid event is a missed incident until someone replays the DLQ"),
    SLO("mcp-tool-latency", "agent tool calls answered within 200 ms (client-observed, incl. transport)", 0.95,
        "db", 0.2, "s", "an investigation makes ~10 tool calls; 200 ms each keeps tools < 5% of the 90 s budget"),
    SLO("mcp-tool-availability", "tool calls without a server fault (INTERNAL/UNAVAILABLE)", 0.999, "db",
        why="a server fault ends an agent's run in escalation, which costs a human review"),
    SLO("investigation-latency", "investigations finished within 90 s, excluding human review", 0.95, "db", 90.0, "s",
        "reviewers work a queue; 90 s keeps a case ready before they reach it"),
    SLO("investigation-completion", "runs that end in a decision for a human (awaiting approval or escalated)",
        0.99, "db", why="a 'failed' or stuck run leaves a case with nobody looking at it"),
    SLO("investigation-cost", "investigations costing at most $0.40 (hard cap $0.50)", 0.95, "db", 0.40, "USD",
        "the design estimate is ~$0.26 on Opus 5.5; the cap stops runaways, this catches drift before the cap"),
    SLO("api-case-load", "case-detail API responses within 500 ms", 0.95, "metrics", 0.5, "s",
        "the reviewer UI's main page; slower and reviewers stop trusting it"),
]
BY_NAME = {s.name: s for s in SLOS}


@dataclass
class SLOResult:
    name: str
    sli: str
    objective: float
    good: int
    total: int
    threshold: float | None = None
    unit: str = ""
    percentiles: dict[str, float] = field(default_factory=dict)
    note: str = ""

    @property
    def compliance(self) -> float | None:
        return self.good / self.total if self.total else None

    @property
    def met(self) -> bool | None:
        c = self.compliance
        return None if c is None else c >= self.objective - 1e-12

    @property
    def budget_used(self) -> float | None:
        """Share of the error budget spent: (bad fraction) / (1 - objective)."""
        c = self.compliance
        if c is None:
            return None
        allowed = 1 - self.objective
        return (1 - c) / allowed if allowed > 0 else (0.0 if c == 1 else float("inf"))

    def as_dict(self) -> dict[str, Any]:
        return {**asdict(self), "compliance": self.compliance, "met": self.met, "budget_used": self.budget_used}


def percentiles(values: list[float]) -> dict[str, float]:
    if not values:
        return {}
    v = sorted(values)

    def q(p: float) -> float:
        # nearest-rank: the smallest value with at least p of the samples at or below it
        # round() first: 0.95 * 20 is 19.000000000000004 in floating point, and ceil would give 20.
        return v[max(0, min(len(v) - 1, math.ceil(round(p * len(v), 9)) - 1))]

    return {"p50": q(0.50), "p95": q(0.95), "p99": q(0.99), "max": v[-1]}


def _ratio(slo: SLO, values: list[float], note: str = "") -> SLOResult:
    thr = slo.threshold if slo.threshold is not None else float("inf")
    return SLOResult(slo.name, slo.sli, slo.objective, sum(1 for x in values if x <= thr), len(values),
                     slo.threshold, slo.unit, percentiles(values), note)


# ---------------------------------------------------------------- sources --- #

def _has(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)).fetchone() is not None


def tool_latencies_s(conn: sqlite3.Connection) -> list[float]:
    if not _has(conn, "audit_trace"):
        return []
    return [r[0] / 1000 for r in conn.execute("SELECT latency_ms FROM audit_trace WHERE kind = 'tool_call'")]


def tool_faults(conn: sqlite3.Connection) -> tuple[int, int]:
    """(calls without a server fault, all tool calls)."""
    if not _has(conn, "audit_trace"):
        return 0, 0
    total = conn.execute("SELECT COUNT(*) FROM audit_trace WHERE kind = 'tool_call'").fetchone()[0]
    marks = ",".join("?" * len(SERVER_FAULT_CODES))
    bad = conn.execute(f"SELECT COUNT(*) FROM audit_trace WHERE kind = 'tool_call' AND status = 'error' AND "
                       f"json_extract(output_json, '$.code') IN ({marks})", SERVER_FAULT_CODES).fetchone()[0]
    return total - bad, total


def runs(conn: sqlite3.Connection, now_ms: int) -> list[dict[str, Any]]:
    if not _has(conn, "investigation_runs"):
        return []
    out = []
    for r in conn.execute("SELECT status, started_at_ms, ended_at_ms, cost_usd FROM investigation_runs "
                          "WHERE status != 'skipped'"):
        status, start, end, cost = r
        if status == "running":
            if now_ms - start < STUCK_RUN_MS:
                continue  # in flight, not yet judged
            status = "stuck"
        out.append({"status": status, "latency_s": ((end or now_ms) - start) / 1000, "cost_usd": cost})
    return out


def queue_outcomes(conn: sqlite3.Connection) -> tuple[int, int, dict[str, int]]:
    """(good, valid, excluded-by-reason) from the broker's tables."""
    if not _has(conn, "queue_messages"):
        return 0, 0, {}
    done = conn.execute("SELECT COUNT(*) FROM queue_messages WHERE state = 'done'").fetchone()[0]
    dead = dict(conn.execute("SELECT reason, COUNT(*) FROM dead_letters WHERE replayed_at_ms IS NULL "
                             "GROUP BY reason").fetchall())
    excluded = {k: v for k, v in dead.items() if k in ("schema", "conflict")}
    bad = sum(v for k, v in dead.items() if k not in excluded)
    return done, done + bad, excluded


# ------------------------------------------------------------- evaluation --- #

def evaluate(dbs: list[sqlite3.Connection], queues: list[sqlite3.Connection] | None = None, *,
             use_metrics: bool = True, now_ms: Callable[[], int] = lambda: int(time.time() * 1000)
             ) -> list[SLOResult]:
    """Pool every given database (e.g. all samples of an eval run) and evaluate each SLO.
    An SLO with no data is reported with total=0 (met=None), never as passing."""
    queues = queues or []
    lat = [x for c in dbs for x in tool_latencies_s(c)]
    faults = [tool_faults(c) for c in dbs]
    rs = [r for c in dbs for r in runs(c, now_ms())]
    qo = [queue_outcomes(c) for c in queues]

    res: dict[str, SLOResult] = {}
    s = BY_NAME["ingest-latency"]
    res[s.name] = _ratio(s, m.INGEST_LATENCY.samples() if use_metrics else [],
                         "" if use_metrics else "needs a running process: use --bench")
    s = BY_NAME["ingest-availability"]
    excl: dict[str, int] = {}
    for _, _, e in qo:
        for k, v in e.items():
            excl[k] = excl.get(k, 0) + v
    res[s.name] = SLOResult(s.name, s.sli, s.objective, sum(g for g, _, _ in qo), sum(t for _, t, _ in qo),
                            note=f"excluded producer errors: {excl}" if excl else "")
    s = BY_NAME["mcp-tool-latency"]
    res[s.name] = _ratio(s, lat)
    s = BY_NAME["mcp-tool-availability"]
    res[s.name] = SLOResult(s.name, s.sli, s.objective, sum(g for g, _ in faults), sum(t for _, t in faults))
    s = BY_NAME["investigation-latency"]
    res[s.name] = _ratio(s, [r["latency_s"] for r in rs if r["status"] != "stuck"])
    s = BY_NAME["investigation-completion"]
    by_status: dict[str, int] = {}
    for r in rs:
        by_status[r["status"]] = by_status.get(r["status"], 0) + 1
    res[s.name] = SLOResult(s.name, s.sli, s.objective,
                            sum(1 for r in rs if r["status"] in ("awaiting_approval", "escalated")), len(rs),
                            note=json.dumps(by_status, sort_keys=True) if rs else "")
    s = BY_NAME["investigation-cost"]
    res[s.name] = _ratio(s, [r["cost_usd"] for r in rs])
    s = BY_NAME["api-case-load"]
    res[s.name] = _ratio(s, m.API_LATENCY.samples(route="/api/cases/{case_id}") if use_metrics else [],
                         "" if use_metrics else "needs a running process: use --bench")
    return [res[x.name] for x in SLOS]


def render(results: list[SLOResult], title: str = "SLO report") -> str:
    def fmt(x: float | None, unit: str) -> str:
        if x is None:
            return "-"
        if unit == "s":
            return f"{x * 1000:.0f} ms" if x < 1 else f"{x:.1f} s"
        if unit == "USD":
            return f"${x:.3f}"
        return f"{x:g}"

    lines = [f"## {title}", "", "| SLO | objective | good / total | compliance | error budget used | p50 | p95 | p99 "
             "| status |", "|---|---|---|---|---|---|---|---|---|"]
    for r in results:
        p = r.percentiles
        status = "no data" if r.met is None else ("met" if r.met else "**MISSED**")
        comp = "-" if r.compliance is None else f"{r.compliance:.2%}"
        bud = "-" if r.budget_used is None else f"{r.budget_used:.0%}"
        lines.append(f"| {r.name} | {r.objective:.1%} | {r.good} / {r.total} | {comp} | {bud} | "
                     f"{fmt(p.get('p50'), r.unit)} | {fmt(p.get('p95'), r.unit)} | {fmt(p.get('p99'), r.unit)} | "
                     f"{status} |")
    notes = [f"- **{r.name}**: {r.note}" for r in results if r.note]
    if notes:
        lines += ["", *notes]
    return "\n".join(lines) + "\n"
