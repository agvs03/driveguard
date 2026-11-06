"""DriveGuard CLI.

  driveguard simulate --out data/run1 [--seed 7 --trips 20]
  driveguard ingest   --data data/run1 [--faults] [--real-time]
  driveguard dlq list | dlq replay [--reason R]
  driveguard drain                      # process queued/replayed messages, then sweep
  driveguard detector-report --data data/run1
  driveguard serve trips|risk|cases [--port N]      # MCP over HTTP; needs DRIVEGUARD_TOKEN_SECRET
  driveguard token --sub S --role R --scopes a,b [--case-id C --trip-ids T1,T2]
  driveguard api --db data/driveguard.db           # reviewer API + UI on :8100; needs DRIVEGUARD_TOKEN_SECRET
  driveguard slo --db "data/evals/runs/<id>/*.db"  # SLO report on stored runs;  slo --bench for a local load run
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import logging
import time
from dataclasses import asdict
from pathlib import Path

from driveguard.clock import FakeClock, SystemClock
from driveguard.events import Event
from driveguard.evals.detector_eval import evaluate_detector
from driveguard.pipeline.broker import SqliteBroker
from driveguard.pipeline.consumer import Consumer, ConsumerConfig
from driveguard.pipeline.runner import ingest_batches
from driveguard.sim.faults import FaultProfile, plan_faults
from driveguard.sim.generator import Driver, Label, SimConfig, Vehicle, simulate
from driveguard.store.repository import Store


def _cmd_simulate(a: argparse.Namespace) -> None:
    out = simulate(SimConfig(seed=a.seed, n_trips=a.trips, n_drivers=a.drivers))
    d = Path(a.out)
    d.mkdir(parents=True, exist_ok=True)
    with open(d / "events.jsonl", "w", encoding="utf-8") as f:
        for e in out.events:
            f.write(e.to_json() + "\n")
    with open(d / "labels.jsonl", "w", encoding="utf-8") as f:
        for lab in out.labels:
            f.write(json.dumps(asdict(lab)) + "\n")
    (d / "reference.json").write_text(json.dumps({
        "drivers": [asdict(x) for x in out.drivers], "vehicles": [asdict(x) for x in out.vehicles],
    }, indent=2), encoding="utf-8")
    print(json.dumps({"events": len(out.events), "labels": len(out.labels), "out": str(d)}))


def _load_labels(d: Path) -> list[Label]:
    labels = []
    for line in (d / "labels.jsonl").read_text(encoding="utf-8").splitlines():
        raw = json.loads(line)
        raw["evidence_event_ids"] = tuple(raw["evidence_event_ids"])
        labels.append(Label(**raw))
    return labels


async def _ingest(a: argparse.Namespace) -> None:
    d = Path(a.data)
    clock = SystemClock() if a.real_time else FakeClock()
    store = await Store(a.db).connect()
    broker = await SqliteBroker(a.queue, clock).connect()
    try:
        ref = json.loads((d / "reference.json").read_text(encoding="utf-8"))
        await store.seed_reference([Driver(**x) for x in ref["drivers"]], [Vehicle(**x) for x in ref["vehicles"]])
        lines = (d / "events.jsonl").read_text(encoding="utf-8").splitlines()
        cfg = ConsumerConfig(jitter_seed=a.fault_seed)
        hook = None
        if a.faults:
            events = [Event.model_validate_json(x) for x in lines]
            plan = plan_faults(events, FaultProfile(seed=a.fault_seed, exhausting_p=0.002, permanent_p=0.002),
                               cfg.max_attempts)
            batches, hook = plan.batches, plan.hook()
        else:
            batches = [lines]
        consumer = Consumer(broker, store, clock, cfg, fault_hook=hook)
        t0 = time.perf_counter()
        await ingest_batches(batches, broker=broker, consumer=consumer, store=store, clock=clock,
                             quiet_s=2.0 if a.real_time else 120.0)
        wall = time.perf_counter() - t0
        msgs = sum(len(b) for b in batches)
        print(json.dumps({
            "messages": msgs, "wall_s": round(wall, 2), "msgs_per_s": round(msgs / wall, 1),
            "consumer": consumer.stats.as_dict(), "store": await store.stats(),
            "queue": await broker.counts(cfg.topic),
        }, indent=2))
    finally:
        await broker.close()
        await store.close()


async def _dlq(a: argparse.Namespace) -> None:
    broker = await SqliteBroker(a.queue, SystemClock()).connect()
    try:
        if a.dlq_cmd == "list":
            for dl in await broker.list_dead():
                print(json.dumps({"msg_id": dl.msg_id, "reason": dl.reason, "attempts": dl.attempts,
                                  "error": (dl.last_error or "")[:160]}))
        else:
            print(json.dumps({"replayed": await broker.replay_dead(reason=a.reason)}))
    finally:
        await broker.close()


async def _drain(a: argparse.Namespace) -> None:
    """Process whatever is already queued (e.g. after `dlq replay`), then sweep."""
    clock = FakeClock()
    store = await Store(a.db).connect()
    broker = await SqliteBroker(a.queue, clock).connect()
    try:
        consumer = Consumer(broker, store, clock, ConsumerConfig())
        await ingest_batches([[]], broker=broker, consumer=consumer, store=store, clock=clock)
        print(json.dumps({"consumer": consumer.stats.as_dict(), "store": await store.stats()}, indent=2))
    finally:
        await broker.close()
        await store.close()


async def _detector_report(a: argparse.Namespace) -> None:
    store = await Store(a.db).connect()
    try:
        print(json.dumps(evaluate_detector(_load_labels(Path(a.data)), await store.list_incidents()), indent=2))
    finally:
        await store.close()


def _llm(a: argparse.Namespace):
    if a.llm == "claude":
        from driveguard.agents.llm import ClaudeClient
        return ClaudeClient(a.model)
    from driveguard.agents.scripted import ScriptedClient
    return ScriptedClient()


async def _agents(a: argparse.Namespace) -> None:
    from dataclasses import replace as dc_replace

    from driveguard.agents.budget import Limits
    from driveguard.agents.runtime import local_orchestrator
    from driveguard.agents.scripted import ScriptedClient

    if a.cmd == "review":
        async with local_orchestrator(a.db, ScriptedClient()) as orch:
            case = await orch.review(a.case, a.decision, a.comment, a.reviewer)
        print(json.dumps({"case_id": case["case_id"], "status": case["status"], "version": case["version"]}))
        return
    limits = dc_replace(Limits(), max_cost_usd=a.max_cost)
    async with local_orchestrator(a.db, _llm(a), limits=limits) as orch:
        # Selected only after the orchestrator has connected, which runs schema migrations:
        # an older database has no `cases` table until then (found in the Phase 3 walkthrough).
        ids = [a.incident] if a.incident else await orch.db.run(lambda c: [r[0] for r in c.execute(
            "SELECT i.incident_id FROM detected_incidents i LEFT JOIN cases k USING (incident_id) "
            "WHERE i.superseded_at_ms IS NULL AND k.case_id IS NULL ORDER BY i.t_start_ms LIMIT ?", (a.limit,))])
        for inc in ids:
            r = await orch.investigate(inc)
            print(json.dumps({"incident_id": inc, "case_id": r.case_id, "status": r.status, "reason": r.reason,
                              "classification": (r.report or {}).get("classification"),
                              "citations_ok": (r.validation or {}).get("ok"), "cost_usd": r.cost_usd,
                              "model_calls": r.model_calls, "tool_calls": r.tool_calls, "latency_s": r.latency_s}))


def _slo(a: argparse.Namespace) -> None:
    import glob
    import sqlite3

    from driveguard.obs.slo import evaluate, render
    if a.bench:
        from driveguard.obs.bench import run_bench
        info = asyncio.run(run_bench(a.work, rate=a.rate, seconds=a.seconds, investigations=a.investigations,
                                     api_requests=a.api_requests, faults=a.faults))
        results = info.pop("results")
        title = a.title or (f"SLO report: local benchmark ({a.rate:g} events/s, scripted model"
                            f"{', ingestion faults injected' if a.faults else ''})")
        print(json.dumps({k: v for k, v in info.items() if k != "slos"}, indent=2), file=sys.stderr)
    else:
        paths = [p for pat in a.db for p in sorted(glob.glob(pat))]
        queues = [p for pat in a.queue for p in sorted(glob.glob(pat))]
        if not paths and not queues:
            raise SystemExit("give --db and/or --queue (or --bench)")
        conns = [sqlite3.connect(Path(p).resolve().as_uri() + "?mode=ro", uri=True) for p in paths]
        qconns = [sqlite3.connect(Path(p).resolve().as_uri() + "?mode=ro", uri=True) for p in queues]
        results = evaluate(conns, qconns, use_metrics=False)
        title = a.title or f"SLO report: {len(paths)} database(s), {len(queues)} queue(s)"
    if a.json:
        print(json.dumps([r.as_dict() for r in results], indent=2))
    else:
        print(render(results, title))


def _inspect(a: argparse.Namespace) -> None:
    import sqlite3

    from driveguard.agents.report import reconstruct
    from driveguard.agents.trace import load_spans, verify_run

    conn = sqlite3.connect(a.db)
    if a.cmd == "report":
        row = conn.execute("SELECT report_id, markdown FROM reports WHERE case_id = ? ORDER BY created_at_ms DESC "
                           "LIMIT 1", (a.case,)).fetchone()
        if not row:
            raise SystemExit("no report for that case")
        print(json.dumps(reconstruct(conn, row[0]), indent=2, default=str) if a.reconstruct else row[1])
        return
    runs = [r[0] for r in conn.execute("SELECT DISTINCT run_id FROM audit_trace WHERE case_id = ? ORDER BY run_id",
                                       (a.case,))]
    for run in runs:
        ok, bad = verify_run(conn, run)
        print(f"run {run}  chain={'VALID' if ok else f'BROKEN at seq {bad}'}")
        for s in load_spans(conn, run):
            tool = s["input"].get("tool", "") if isinstance(s["input"], dict) else ""
            line = (f"  {s['seq']:>3} {s['kind']:<14} {s['actor']:<26} {s['status']:<5} {tool:<28} "
                    f"{s['model'] or '':<20} ${s['cost_usd']:.4f}")
            print(line)
            if a.full:
                print("      in :", json.dumps(s["input"], default=str)[:300])
                print("      out:", json.dumps(s["output"], default=str)[:300])


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="driveguard")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("simulate")
    s.add_argument("--out", required=True)
    s.add_argument("--seed", type=int, default=7)
    s.add_argument("--trips", type=int, default=20)
    s.add_argument("--drivers", type=int, default=8)

    for name in ("ingest", "dlq", "drain", "detector-report"):
        sp = sub.add_parser(name)
        sp.add_argument("--db", default="data/driveguard.db")
        sp.add_argument("--queue", default="data/queue.db")
        if name == "ingest":
            sp.add_argument("--data", required=True)
            sp.add_argument("--faults", action="store_true")
            sp.add_argument("--fault-seed", type=int, default=1)
            sp.add_argument("--real-time", action="store_true", help="use the wall clock instead of simulated time")
        elif name == "dlq":
            sp.add_argument("dlq_cmd", choices=["list", "replay"])
            sp.add_argument("--reason")
        elif name == "detector-report":
            sp.add_argument("--data", required=True)

    sv = sub.add_parser("serve", help="run an MCP server over HTTP (needs DRIVEGUARD_TOKEN_SECRET)")
    sv.add_argument("server", choices=["trips", "risk", "cases"])
    sv.add_argument("--db", default="data/driveguard.db")
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int)

    tk = sub.add_parser("token", help="issue a bearer token (needs DRIVEGUARD_TOKEN_SECRET)")
    tk.add_argument("--sub", required=True)
    tk.add_argument("--role", required=True, choices=["agent", "human", "service"])
    tk.add_argument("--scopes", required=True, help="comma-separated, e.g. trips:read,cases:read")
    tk.add_argument("--aud", default="trips,risk,cases", help="comma-separated server names")
    tk.add_argument("--case-id")
    tk.add_argument("--trip-ids", default="", help="comma-separated")
    tk.add_argument("--ttl", type=int, default=900)

    inv = sub.add_parser("investigate", help="run the agent team on detected incidents")
    inv.add_argument("--db", default="data/driveguard.db")
    g = inv.add_mutually_exclusive_group(required=True)
    g.add_argument("--incident")
    g.add_argument("--all", action="store_true", help="every active incident without a case")
    inv.add_argument("--limit", type=int, default=10)
    inv.add_argument("--llm", choices=["scripted", "claude"], default="scripted")
    inv.add_argument("--model", default="claude-opus-5-5")
    inv.add_argument("--max-cost", type=float, default=0.50, help="hard $ cap per case")

    rv = sub.add_parser("review", help="human approval: approve or reject a case awaiting approval")
    rv.add_argument("--db", default="data/driveguard.db")
    rv.add_argument("--case", required=True)
    rv.add_argument("--decision", required=True, choices=["approve", "reject"])
    rv.add_argument("--comment", required=True)
    rv.add_argument("--reviewer", required=True)

    tr = sub.add_parser("trace", help="show and verify a case's audit trace")
    tr.add_argument("--db", default="data/driveguard.db")
    tr.add_argument("--case", required=True)
    tr.add_argument("--full", action="store_true")

    rp = sub.add_parser("report", help="print the latest report for a case (markdown)")
    rp.add_argument("--db", default="data/driveguard.db")
    rp.add_argument("--case", required=True)
    rp.add_argument("--reconstruct", action="store_true", help="show each citation's source tool call")

    from driveguard.evals import cli as eval_cli
    eval_cli.add_parser(sub)

    rt = sub.add_parser("redteam", help="run the attack suite with guardrails off and on")
    rt.add_argument("--db", default="data/evals/dev.db", help="base database (copied per attack)")
    rt.add_argument("--work", default="data/redteam")
    rt.add_argument("--out", default="data/redteam/results.md")
    rt.add_argument("--llm", choices=["gullible", "claude"], default="gullible")
    rt.add_argument("--model", default="claude-opus-5-5")
    rt.add_argument("--attacks", help="comma-separated subset")
    rt.add_argument("--modes", default="off,on")
    rt.add_argument("--max-cost", type=float, default=3.0, help="hard $ cap for --llm claude")
    rt.add_argument("--env-file", help="read ANTHROPIC_API_KEY/WORKSPACE_ID from this file (never printed)")

    ap = sub.add_parser("api", help="serve the read-only reviewer API and UI (needs DRIVEGUARD_TOKEN_SECRET)")
    ap.add_argument("--db", default="data/driveguard.db")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8100)
    ap.add_argument("--static", default="web/dist", help="built UI directory (served at / if present)")

    so = sub.add_parser("slo", help="evaluate the SLOs on stored data, or run the local benchmark")
    so.add_argument("--db", action="append", default=[], help="DriveGuard database(s), pooled; glob patterns ok")
    so.add_argument("--queue", action="append", default=[], help="broker queue database(s)")
    so.add_argument("--bench", action="store_true", help="run ingest/agents/API load locally, then evaluate")
    so.add_argument("--work", default="data/bench")
    so.add_argument("--rate", type=float, default=500, help="bench: events per second to publish")
    so.add_argument("--seconds", type=float, default=20)
    so.add_argument("--investigations", type=int, default=12)
    so.add_argument("--api-requests", type=int, default=300)
    so.add_argument("--faults", action="store_true", help="bench: inject the Phase 1 fault profile into ingestion")
    so.add_argument("--title", default=None)
    so.add_argument("--json", action="store_true")

    p.add_argument("-v", "--verbose", action="store_true", help="log at INFO (default: errors only)")
    p.add_argument("--log-format", choices=["json", "text"], default="json", help="log lines on stderr")
    p.add_argument("--trace-file", help="append finished telemetry spans to this JSONL file")
    a = p.parse_args(argv)
    from driveguard.obs.logging import configure_logging
    configure_logging(logging.INFO if a.verbose else logging.ERROR, a.log_format)
    if a.trace_file:
        from driveguard.obs.tracing import JsonlExporter, add_exporter
        add_exporter(JsonlExporter(a.trace_file))
    if a.cmd == "simulate":
        _cmd_simulate(a)
    elif a.cmd == "ingest":
        Path(a.db).parent.mkdir(parents=True, exist_ok=True)
        asyncio.run(_ingest(a))
    elif a.cmd == "dlq":
        asyncio.run(_dlq(a))
    elif a.cmd == "drain":
        asyncio.run(_drain(a))
    elif a.cmd in ("investigate", "review"):
        asyncio.run(_agents(a))
    elif a.cmd in ("trace", "report"):
        _inspect(a)
    elif a.cmd == "api":
        from driveguard.api.app import serve as serve_api
        serve_api(a.db, a.host, a.port, a.static)
    elif a.cmd == "slo":
        _slo(a)
    elif a.cmd == "eval":
        from driveguard.evals import cli as eval_cli
        eval_cli.main(a)
    elif a.cmd == "redteam":
        from driveguard.guardrails import OFF, ON
        from driveguard.redteam.attacks import render, run_suite
        factory = None
        if a.llm == "claude":
            from driveguard.agents.llm import ClaudeClient
            from driveguard.evals.cli import load_env_file
            if a.env_file:
                print(f"loaded from env file: {load_env_file(a.env_file)}", file=sys.stderr)
            factory = lambda: ClaudeClient(a.model)  # noqa: E731
        modes = tuple({"off": OFF, "on": ON}[m] for m in a.modes.split(","))
        results = asyncio.run(run_suite(Path(a.db), Path(a.work), modes, factory,
                                        set(a.attacks.split(",")) if a.attacks else None,
                                        a.max_cost if a.llm == "claude" else None))
        md = render(results)
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_text(md, encoding="utf-8")
        Path(a.out).with_suffix(".json").write_text(json.dumps(results, indent=1, default=str), encoding="utf-8")
        print(md)
    elif a.cmd == "serve":
        from driveguard.mcp_servers.serve import serve
        serve(a.server, a.db, a.host, a.port)
    elif a.cmd == "token":
        from driveguard.mcp_servers.auth import AUD, TokenIssuer
        from driveguard.mcp_servers.serve import require_secret
        try:
            print(TokenIssuer(require_secret()).issue(
                sub=a.sub, role=a.role, scopes=[s for s in a.scopes.split(",") if s],
                audiences=[AUD[x] for x in a.aud.split(",") if x], case_id=a.case_id,
                trip_ids=[t for t in a.trip_ids.split(",") if t], ttl_s=a.ttl))
        except (ValueError, KeyError) as e:
            raise SystemExit(f"refused: {e}") from None
    else:
        asyncio.run(_detector_report(a))


if __name__ == "__main__":
    main()
