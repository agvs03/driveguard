"""Local load benchmark that exercises every SLO end to end, then evaluates them.

    driveguard slo --bench [--rate 500 --seconds 20 --investigations 12 --api-requests 300]

1. ingest   publish synthetic events at a fixed rate (real clock) while the
            consumer drains concurrently; publish-to-durable latency lands in
            the ingest histogram
2. detect   sweep the trips so incidents are detected
3. agents   run scripted investigations over in-process MCP servers (tool
            latency and run outcomes go to the audit trace)
4. api      request the case list and every case's detail page through the
            ASGI app (server-side latency; browser render time is not included)

Honest limits: it runs on one laptop with SQLite and the free scripted model,
so it measures our code paths, not Claude's latency. Real-model latency and
cost come from evaluating the stored eval runs (`driveguard slo --db ...`).
"""

from __future__ import annotations

import asyncio
import json
import secrets
import sqlite3
import time
from pathlib import Path
from typing import Any

from driveguard.clock import SystemClock
from driveguard.obs.metrics import REGISTRY
from driveguard.obs.slo import evaluate
from driveguard.pipeline.broker import SqliteBroker
from driveguard.pipeline.consumer import Consumer, ConsumerConfig
from driveguard.sim.generator import SimConfig, simulate
from driveguard.store.repository import Store


async def _ingest(db: Path, queue: Path, rate: float, seconds: float, seed: int, faults: bool) -> dict[str, Any]:
    out = simulate(SimConfig(seed=seed, n_trips=40))
    events = sorted(out.events, key=lambda e: e.event_time_ms)[:int(rate * seconds)]
    hook = None
    if faults:
        # Phase 1's fault profile (duplicates, reordering, corrupt bodies, 5% transient failures, a few
        # poison messages). The late batch is sent last, straight after the rest: lateness needs quiet
        # time between batches, which a rate benchmark does not have.
        from driveguard.sim.faults import FaultProfile, plan_faults
        plan = plan_faults(events, FaultProfile(seed=seed, exhausting_p=0.002, permanent_p=0.002))
        lines, hook = [b for batch in plan.batches for b in batch], plan.hook()
    else:
        lines = [e.to_json() for e in events]
    clock = SystemClock()
    store = await Store(str(db)).connect()
    broker = await SqliteBroker(str(queue), clock).connect()
    try:
        await store.seed_reference(out.drivers, out.vehicles)
        consumer = Consumer(broker, store, clock, ConsumerConfig(batch_size=128, concurrency=16), fault_hook=hook)
        stop = asyncio.Event()
        worker = asyncio.create_task(consumer.run_forever(stop, idle_poll_s=0.02))
        tick = 0.05
        per_tick = max(1, int(rate * tick))
        t0 = time.perf_counter()
        for i in range(0, len(lines), per_tick):
            await broker.publish_many(consumer.cfg.topic, [(None, b) for b in lines[i:i + per_tick]])
            # pace against the schedule, not the previous tick, so slow ticks don't lower the rate
            delay = t0 + (i // per_tick + 1) * tick - time.perf_counter()
            if delay > 0:
                await asyncio.sleep(delay)
        publish_s = time.perf_counter() - t0
        while True:
            c = await broker.counts(consumer.cfg.topic)
            if c.get("ready", 0) + c.get("inflight", 0) == 0:
                break
            await asyncio.sleep(0.05)
        drain_s = time.perf_counter() - t0
        stop.set()
        await worker
        swept = await store.sweep(clock.now_ms() + 10 ** 9, 0)  # finalize every trip so incidents are detected
        return {"messages": len(lines), "faults": faults, "target_rate": rate, "achieved_publish_rate": round(len(lines) / publish_s, 1),
                "drain_s": round(drain_s, 2), "trips_swept": len(set(swept)), "consumer": consumer.stats.as_dict()}
    finally:
        await broker.close()
        await store.close()


async def _investigate(db: Path, n: int) -> dict[str, Any]:
    from driveguard.agents.runtime import local_orchestrator
    from driveguard.agents.scripted import ScriptedClient

    statuses: dict[str, int] = {}
    async with local_orchestrator(str(db), ScriptedClient()) as orch:
        ids = await orch.db.run(lambda c: [r[0] for r in c.execute(
            "SELECT incident_id FROM detected_incidents WHERE superseded_at_ms IS NULL ORDER BY t_start_ms LIMIT ?",
            (n,))])
        for inc in ids:
            r = await orch.investigate(inc)
            statuses[r.status] = statuses.get(r.status, 0) + 1
    return {"investigations": len(ids), "statuses": statuses}


async def _api(db: Path, requests: int) -> dict[str, Any]:
    import httpx2

    from driveguard.api.app import create_app
    from driveguard.mcp_servers.auth import AUD, TokenIssuer

    secret = secrets.token_urlsafe(48)
    app = create_app(str(db), secret)
    tok = TokenIssuer(secret).issue(sub="human:bench", role="human", scopes={"cases:read", "trace:read"},
                                    audiences=[AUD["api"]])
    h = {"Authorization": f"Bearer {tok}"}
    n = 0
    async with app.router.lifespan_context(app):
        async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://bench") as c:
            ids = [x["case_id"] for x in (await c.get("/api/cases", headers=h)).json()["cases"]]
            while ids and n < requests:
                for cid in ids:
                    r = await c.get(f"/api/cases/{cid}", headers=h)
                    r.raise_for_status()
                    n += 1
                    if n >= requests:
                        break
    return {"case_detail_requests": n, "cases": len(ids)}


async def run_bench(workdir: str | Path, *, rate: float = 500, seconds: float = 20, investigations: int = 12,
                    api_requests: int = 300, seed: int = 11, faults: bool = False) -> dict[str, Any]:
    work = Path(workdir)
    work.mkdir(parents=True, exist_ok=True)
    db, queue = work / "bench.db", work / "bench-queue.db"
    for p in work.glob("bench*.db*"):
        p.unlink()
    REGISTRY.reset()
    info = {"ingest": await _ingest(db, queue, rate, seconds, seed, faults),
            "agents": await _investigate(db, investigations),
            "api": await _api(db, api_requests)}
    with sqlite3.connect(db) as c, sqlite3.connect(queue) as q:
        results = evaluate([c], [q])
    info["slos"] = [r.as_dict() for r in results]
    (work / ("bench-faults.json" if faults else "bench.json")).write_text(json.dumps(info, indent=2, default=str), encoding="utf-8")
    return {**info, "results": results}
