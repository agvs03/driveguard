"""Eval runner: k samples x every case, each sample on a fresh copy of the
dataset database (cases are one-per-incident, so samples cannot share a DB).

Every run is stored with: label, LLM/model, dataset hash, code fingerprint
(hash of driveguard/**/*.py, since there are no commits yet), git sha when
available, limits, and per-case results. Per-sample databases are kept under
data/evals/runs/<run_id>/ so any failure can be inspected with
`driveguard trace --db ... --case ...`.

A run-level dollar cap stops scheduling new cases once the remaining budget
cannot cover one more case at its per-case cap; unscheduled cases are stored
as `skipped_budget` and excluded from the metrics (and reported).
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import time
import uuid
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any, Callable

from driveguard.agents.budget import Limits
from driveguard.agents.llm import LLMClient
from driveguard.agents.runtime import local_orchestrator
from driveguard.agents.trace import load_spans
from driveguard.evals.dataset import Dataset, EvalCase, ensure_db
from driveguard.evals.metrics import CaseResult, score_case

PKG = Path(__file__).resolve().parents[1]
EVAL_SCHEMA = """
CREATE TABLE IF NOT EXISTS eval_runs (
    run_id TEXT PRIMARY KEY, label TEXT NOT NULL, dataset TEXT NOT NULL, dataset_hash TEXT NOT NULL,
    llm TEXT NOT NULL, samples INTEGER NOT NULL, code_fingerprint TEXT NOT NULL, git_sha TEXT,
    config_json TEXT NOT NULL, started_at_ms INTEGER NOT NULL, ended_at_ms INTEGER, total_cost_usd REAL,
    summary_json TEXT
);
CREATE TABLE IF NOT EXISTS eval_results (
    run_id TEXT NOT NULL REFERENCES eval_runs (run_id), case_key TEXT NOT NULL, sample INTEGER NOT NULL,
    result_json TEXT NOT NULL, PRIMARY KEY (run_id, case_key, sample)
);
"""


def code_fingerprint() -> str:
    h = hashlib.sha256()
    for p in sorted(PKG.rglob("*.py")) + sorted(PKG.rglob("*.json")) + sorted(PKG.rglob("*.sql")):
        h.update(str(p.relative_to(PKG)).encode())
        h.update(p.read_bytes().replace(b"\r\n", b"\n"))
    return h.hexdigest()[:16]


def git_sha() -> str | None:
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=PKG.parent, capture_output=True,
                             text=True, timeout=5)
        sha = out.stdout.strip() if out.returncode == 0 else None
        dirty = subprocess.run(["git", "status", "--porcelain"], cwd=PKG.parent, capture_output=True, text=True,
                               timeout=5).stdout.strip()
        return f"{sha}{'-dirty' if dirty else ''}" if sha else None
    except (OSError, subprocess.SubprocessError):
        return None


def open_eval_db(path: str | Path) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    c.executescript(EVAL_SCHEMA)
    return c


def load_results(conn: sqlite3.Connection, run_id: str) -> list[CaseResult]:
    return [CaseResult(**json.loads(r["result_json"])) for r in conn.execute(
        "SELECT result_json FROM eval_results WHERE run_id = ? ORDER BY case_key, sample", (run_id,))]


def _copy(src: Path, dst: Path) -> Path:
    dst.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(src) as s, sqlite3.connect(dst) as d:
        s.backup(d)
    return dst


async def run_eval(ds: Dataset, *, base_db: str | Path, llm_factory: Callable[[], LLMClient], samples: int,
                   label: str, eval_db: str | Path, runs_dir: str | Path, run_cost_cap: float = 5.0,
                   limits: Limits = Limits(), prices: dict[str, Any] | None = None,
                   cases: list[EvalCase] | None = None, progress: Callable[[str], None] | None = None) -> str:
    base = ensure_db(ds, base_db)
    cases = cases if cases is not None else ds.cases
    run_id = f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    probe = llm_factory()
    conn = open_eval_db(eval_db)
    config = {"limits": asdict(limits), "run_cost_cap": run_cost_cap, "cases": len(cases), "model": probe.model}
    with conn:
        conn.execute("INSERT INTO eval_runs (run_id, label, dataset, dataset_hash, llm, samples, code_fingerprint, "
                     "git_sha, config_json, started_at_ms) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                     (run_id, label, ds.name, ds.dataset_hash, f"{probe.name}:{probe.model}", samples,
                      code_fingerprint(), git_sha(), json.dumps(config), int(time.time() * 1000)))
    spent = 0.0
    for k in range(samples):
        db_k = _copy(base, Path(runs_dir) / run_id / f"sample-{k}.db")
        async with local_orchestrator(str(db_k), llm_factory(), limits=limits, prices=prices) as orch:
            ctx = await orch.db.run(lambda c: (
                {r[0]: r[1] for r in c.execute("SELECT event_id, event_time_ms FROM raw_events")},
                {r[0]: (r[1], r[2]) for r in c.execute(
                    "SELECT incident_id, t_start_ms, t_end_ms FROM detected_incidents")}))
            event_times, windows = ctx
            for case in cases:
                if spent + limits.max_cost_usd > run_cost_cap:
                    res = CaseResult(case_key=case.case_key, sample=k, expected=case.expected,
                                     truth_source=case.truth_source, detected_kind=case.detected_kind,
                                     predicted=None, status="skipped_budget",
                                     reason=f"run cap ${run_cost_cap:.2f} reached", correct=False, decided=False,
                                     bucket="skipped_budget")
                else:
                    outcome = await orch.investigate(case.incident_id)
                    spent += outcome.cost_usd
                    spans = await orch.db.run(load_spans, outcome.run_id)
                    res = score_case(case, k, outcome, spans, event_times, windows[case.incident_id])
                with conn:
                    conn.execute("INSERT INTO eval_results (run_id, case_key, sample, result_json) VALUES (?, ?, ?, ?)",
                                 (run_id, case.case_key, k, json.dumps(asdict(res))))
                if progress:
                    progress(f"sample {k} {case.case_key} {res.status:<18} expected={case.expected:<18} "
                             f"predicted={res.predicted} ${res.cost_usd:.4f}")
    from driveguard.evals.metrics import aggregate
    summary = aggregate(load_results(conn, run_id))
    with conn:
        conn.execute("UPDATE eval_runs SET ended_at_ms = ?, total_cost_usd = ?, summary_json = ? WHERE run_id = ?",
                     (int(time.time() * 1000), round(spent, 6), json.dumps(summary), run_id))
    conn.close()
    return run_id


def with_cost(limits: Limits, max_cost: float) -> Limits:
    return replace(limits, max_cost_usd=max_cost)
