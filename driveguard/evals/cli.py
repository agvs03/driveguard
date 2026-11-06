"""`driveguard eval ...` subcommands.

  eval build   --split dev|test|mini              build DB + dataset JSON (evals/datasets/)
  eval run     --split S --llm scripted|claude --samples K --label L [--max-run-cost 5]
  eval report  --run RUN_ID                       markdown report with error analysis
  eval compare --base RUN --cand RUN              before/after with paired-bootstrap CIs
  eval gate    --split mini [--update]            scripted eval vs committed baseline; exit 1 on regression
  eval list
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from dataclasses import asdict, replace
from pathlib import Path

from driveguard.agents.budget import Limits
from driveguard.evals.dataset import REPO_DATASETS, SPLITS, Dataset, build_dataset
from driveguard.evals.metrics import CaseResult, compare, regressions
from driveguard.evals.report import compare_report, run_meta, run_report
from driveguard.evals.runner import load_results, open_eval_db, run_eval

DATA = Path("data/evals")
EVAL_DB = DATA / "evals.db"
BASELINES = REPO_DATASETS.parent / "baselines"
ALLOWED_ENV = {"ANTHROPIC_API_KEY", "ANTHROPIC_WORKSPACE_ID"}  # nothing else is read from an env file


def load_env_file(path: str) -> list[str]:
    """Load only allow-listed keys from a dotenv-style file into os.environ.
    Values are never printed or logged; returns the key NAMES that were set."""
    loaded = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip().removeprefix("export ").strip()
        if k in ALLOWED_ENV:
            os.environ[k] = v.strip().strip('"').strip("'")
            loaded.append(k)
    return loaded


def _dataset(split: str) -> Dataset:
    p = REPO_DATASETS / f"evalset-v2-{split}.json"
    if not p.exists():
        raise SystemExit(f"{p} missing: run `driveguard eval build --split {split}`")
    return Dataset.load(p)


def _llm_factory(a: argparse.Namespace):
    if a.llm == "claude":
        from driveguard.agents.llm import ClaudeClient
        return lambda: ClaudeClient(a.model)
    from driveguard.agents.scripted import ScriptedClient
    faults = set(filter(None, (a.faults or "").split(",")))
    return lambda: ScriptedClient(faults)


def add_parser(sub: argparse._SubParsersAction) -> None:
    ev = sub.add_parser("eval", help="evaluation harness")
    es = ev.add_subparsers(dest="eval_cmd", required=True)
    b = es.add_parser("build")
    b.add_argument("--split", choices=list(SPLITS), required=True)
    b.add_argument("--per-class", type=int, default=8)
    b.add_argument("--rebuild-db", action="store_true")
    r = es.add_parser("run")
    r.add_argument("--split", choices=list(SPLITS), required=True)
    r.add_argument("--llm", choices=["scripted", "claude"], default="scripted")
    r.add_argument("--model", default="claude-opus-5-5")
    r.add_argument("--faults", help="scripted only: comma-separated fault names (for demos/tests)")
    r.add_argument("--samples", type=int, default=3)
    r.add_argument("--label", required=True)
    r.add_argument("--limit-cases", type=int, help="run only the first N cases (cheap smoke runs)")
    r.add_argument("--max-run-cost", type=float, default=5.0, help="hard $ cap for the whole run")
    r.add_argument("--max-case-cost", type=float, default=0.50)
    r.add_argument("--env-file", help="read ANTHROPIC_API_KEY from this dotenv file (value never printed)")
    r.add_argument("--quiet", action="store_true")
    rp = es.add_parser("report")
    rp.add_argument("--run", required=True)
    rp.add_argument("--out")
    c = es.add_parser("compare")
    c.add_argument("--base", required=True)
    c.add_argument("--cand", required=True)
    c.add_argument("--out")
    g = es.add_parser("gate")
    g.add_argument("--split", default="mini", choices=list(SPLITS))
    g.add_argument("--update", action="store_true", help="accept current results as the new baseline")
    rv = es.add_parser("revalidate", help="re-score a run's first drafts with the current validator ($0)")
    rv.add_argument("--run", required=True)
    es.add_parser("list")


def main(a: argparse.Namespace) -> None:
    if a.eval_cmd == "build":
        ds = build_dataset(a.split, DATA / f"{a.split}.db", a.per_class, reuse_db=not a.rebuild_db)
        REPO_DATASETS.mkdir(parents=True, exist_ok=True)
        out = REPO_DATASETS / f"{ds.name}.json"
        out.write_text(ds.to_json(), encoding="utf-8")
        print(json.dumps({"dataset": str(out), "hash": ds.dataset_hash, "cases": len(ds.cases), **ds.stats}, indent=1))
    elif a.eval_cmd == "run":
        if a.env_file:
            print(f"loaded from env file: {load_env_file(a.env_file) or 'nothing'}", file=sys.stderr)
        if a.llm == "claude" and not os.environ.get("ANTHROPIC_API_KEY"):
            raise SystemExit("ANTHROPIC_API_KEY is not set (use --env-file or export it)")
        ds = _dataset(a.split)
        cases = ds.cases[: a.limit_cases] if a.limit_cases else None
        run_id = asyncio.run(run_eval(
            ds, base_db=DATA / f"{a.split}.db", llm_factory=_llm_factory(a), samples=a.samples, label=a.label,
            eval_db=EVAL_DB, runs_dir=DATA / "runs", run_cost_cap=a.max_run_cost,
            limits=replace(Limits(), max_cost_usd=a.max_case_cost), cases=cases,
            progress=None if a.quiet else (lambda s: print(s, file=sys.stderr))))
        _write_report(run_id, None)
    elif a.eval_cmd == "report":
        _write_report(a.run, a.out)
    elif a.eval_cmd == "compare":
        conn = open_eval_db(EVAL_DB)
        md, bad = compare_report(run_meta(conn, a.base), run_meta(conn, a.cand), load_results(conn, a.base),
                                 load_results(conn, a.cand))
        out = Path(a.out or DATA / "reports" / f"compare-{a.base}-vs-{a.cand}.md")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(md, encoding="utf-8")
        print(md)
        if bad:
            raise SystemExit(1)
    elif a.eval_cmd == "gate":
        gate(a.split, a.update)
    elif a.eval_cmd == "revalidate":
        from driveguard.evals.revalidate import main as revalidate_main
        revalidate_main(str(DATA / "runs"), a.run)
    else:
        conn = open_eval_db(EVAL_DB)
        for r in conn.execute("SELECT run_id, label, dataset, llm, samples, total_cost_usd, summary_json "
                              "FROM eval_runs ORDER BY started_at_ms"):
            s = json.loads(r["summary_json"] or "{}").get("metrics", {})
            acc = (s.get("accuracy") or {}).get("value")
            print(f"{r['run_id']}  {r['label']:<24} {r['dataset']:<20} {r['llm']:<32} k={r['samples']} "
                  f"acc={acc if acc is None else round(acc, 3)} ${r['total_cost_usd'] or 0:.4f}")


def _write_report(run_id: str, out: str | None) -> None:
    conn = open_eval_db(EVAL_DB)
    md = run_report(run_meta(conn, run_id), load_results(conn, run_id))
    path = Path(out or DATA / "reports" / f"{run_id}.md")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(md, encoding="utf-8")
    print(md)
    print(f"[report written to {path}]", file=sys.stderr)


def gate(split: str, update: bool) -> None:
    """Regression gate for 'run the evals on every change'. Uses the free scripted LLM so it
    can run in CI and as a pre-commit hook; real-model evals are run deliberately, not per change."""
    from driveguard.agents.scripted import ScriptedClient

    ds = _dataset(split)
    run_id = asyncio.run(run_eval(ds, base_db=DATA / f"{split}.db", llm_factory=ScriptedClient, samples=1,
                                  label=f"gate-{split}", eval_db=EVAL_DB, runs_dir=DATA / "runs"))
    conn = open_eval_db(EVAL_DB)
    cand = load_results(conn, run_id)
    path = BASELINES / f"scripted-{split}.json"
    if update or not path.exists():
        BASELINES.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps([asdict(r) for r in cand], indent=1), encoding="utf-8")
        print(f"baseline written: {path} ({len(cand)} results)")
        return
    base = [CaseResult(**r) for r in json.loads(path.read_text(encoding="utf-8"))]
    cmp = compare(base, cand)
    bad = regressions(cmp)
    changed = cmp["changed_predictions"]
    print(json.dumps({"run_id": run_id, "regressions": bad, "changed_predictions": changed}, indent=1))
    if bad:
        raise SystemExit(1)
