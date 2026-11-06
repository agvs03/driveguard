# DriveGuard

A multi-agent system that investigates risky driving events, such as hard
braking, crashes, near misses and phone use, and hands a cited, verifiable
report to a human reviewer. It runs on synthetic telematics from ingestion to
review:
- fault-tolerant event ingestion;
- three MCP servers;
- an orchestrator with specialist Claude agents under hard limits;
- a hash-chained audit trace;
- an eval harness and a red-team suite;
- telemetry with SLOs;
- a reviewer UI.

**All data is synthetic.** Drivers, vehicles, trips, notes and phone numbers
come from a seeded simulator. There is no real company, customer or
credential data.

![Case view: report with citation chips, run stats, verified audit chain and the agents' reasoning timeline](docs/img/case-view.jpg)

## Architecture

```mermaid
flowchart LR
  sim[Simulator<br/>seeded, with fault injection] -->|events| broker[(Durable broker<br/>SQLite, leases, DLQ)]
  broker --> consumer[Consumer<br/>idempotent apply, retries]
  consumer --> store[(Store<br/>events, trips, incidents)]
  store --> detector[Detector] --> store
  subgraph MCP servers [MCP servers: JWT auth, typed schemas, error envelope]
    trips[trips]:::srv
    risk[risk]:::srv
    cases[cases]:::srv
  end
  store --- trips & risk & cases
  orch[Orchestrator<br/>code, not an LLM] --> triage[Triage agent] & evidence[Evidence agent] & writer[Report writer<br/>no tools]
  triage & evidence -->|case-bound read tokens| gw[Tool gateway<br/>allow-lists, budgets, guardrails]
  gw --> trips & risk
  orch -->|case writes| cases
  orch --> val[Citation validator<br/>+ output guard]
  orch --> trace[(Audit trace<br/>append-only, hash-chained)]
  api[Read-only API<br/>trace:read is human-only] --> store & trace
  ui[React + TS reviewer UI] --> api
  human[Human reviewer] -->|driveguard review| cases
  classDef srv fill:#eef,stroke:#88a
```

**Invariants** (each one has tests):
- **No agent can close a case.** Agent tokens can't carry `cases:review`,
  enforced both when a token is issued and when it's verified. Only a human
  approves.
- **Least-privilege agents.**
  - Each agent gets its own token, bound to one case and its trip.
  - The report writer has no tools at all.
  - Only the orchestrator writes to cases.
- **Hard limits** on model calls, tool calls, dollars and wall time, checked
  *before* each call. Breaching one escalates the case; nothing is silently
  truncated.
- **Every claim is checkable.** A citation must name an event that a tool call
  in this run actually returned, and every number must match it. One repair
  pass is allowed, then the case escalates.
- **Every step is recorded.** Model calls, tool calls, checks and decisions go
  into an append-only, hash-chained trace, and editing any span breaks the
  chain.

More detail:
- [design](docs/DESIGN.md)
- [decisions and mistakes log](docs/DECISIONS.md)
- [evals](docs/EVALS.md)
- [security and red team](docs/SECURITY.md)
- [operations and SLOs](docs/OPERATIONS.md)

## How to run

New here? [docs/GETTING_STARTED.md](docs/GETTING_STARTED.md) is a guided 10-minute first run.

You need Python 3.10+ and Node 20+. No API key is needed for anything except
the optional real-model runs. The default model is a deterministic scripted
agent.

```bash
python -m venv .venv && .venv/Scripts/activate      # Windows; on macOS/Linux: source .venv/bin/activate
pip install -e ".[dev]"
pytest -m "not slow"                                 # 227 fast tests; `pytest` runs all 270 (~2 min)
```

The whole pipeline, end to end:

```bash
driveguard simulate --out data/run1 --seed 7 --trips 20
driveguard ingest --data data/run1 --db data/driveguard.db --faults   # duplicates, reordering, retries, DLQ
driveguard investigate --db data/driveguard.db --all --limit 5        # scripted agents; --llm claude for real
driveguard trace --db data/driveguard.db --case <case_id>             # shows and verifies the hash chain
driveguard review --db data/driveguard.db --case <case_id> --decision approve --comment "ok" --reviewer you
```

The reviewer UI:

```bash
cd web && npm install && npm run build && cd ..
export DRIVEGUARD_TOKEN_SECRET=$(python -c "import secrets; print(secrets.token_urlsafe(48))")
driveguard token --sub human:you --role human --scopes cases:read,trace:read --aud api   # paste into the UI
driveguard api --db data/driveguard.db              # http://127.0.0.1:8100
```

For UI development, run `npm run dev` in `web/` (port 5173, which proxies
`/api` to 8100). Then run the UI checks with `npm test` and
`npm run typecheck`.

Evals, red team and SLOs:

```bash
driveguard eval run --split dev --samples 3 --label my-change     # metrics with bootstrap CIs
driveguard eval gate                                              # regression gate on `mini`, also run inside pytest
driveguard redteam                                                # 9 attacks, guardrails off vs on
driveguard slo --bench [--faults]                                 # local load run, then the SLO report
driveguard slo --db "data/evals/runs/<run-id>/*.db"               # SLOs on stored runs
```

Real Claude runs are opt-in and capped. For example:

```bash
driveguard eval run --split mini --label claude-run --llm claude --env-file .env
```

This reads only `ANTHROPIC_API_KEY` and `ANTHROPIC_WORKSPACE_ID`, has a $5 cap
per eval run and a $0.50 cap per case, and never prints the key.

## Results

### Accuracy and grounding

Measured with Claude Opus 5.5 on the held-out `mini` split: 10 cases × 2
samples, $4.64. Details in [EVALS.md](docs/EVALS.md).

| | value |
|---|---|
| classification accuracy | **19 / 19** (but a small sample of clear-cut synthetic cases) |
| fabricated event ids in shipped reports | **0** |
| citation precision | 0.96 |
| cost per case | **$0.244** |
| latency p50 / p95 | 58 s / 73 s |

The scripted baseline scores 1.000 on 240 dev and test case-samples. That is a
*ceiling* check of the harness, not a skill measure, because the scripted
agent and the labels follow the same policy.

### Security

Details in [SECURITY.md](docs/SECURITY.md).

| | guardrails off | guardrails on |
|---|---|---|
| 9 attacks, worst-case gullible agent | 3 succeed | **0 succeed** |
| 5 injections against real Claude ($1.85) | 0 succeed | 0 succeed; 3 false escalations, fixed and re-checked to 0 |
| false escalations on 240 clean case-samples | n/a | 0 |

An ablation shows the deterministic **output guard** is the one load-bearing
guardrail against steering attacks. It recomputes the classification from
the data, instead of trying to recognise the attack.

### SLOs

Details in [OPERATIONS.md](docs/OPERATIONS.md).

| SLO | objective | real Claude run | local bench | bench + injected faults |
|---|---|---|---|---|
| investigation within 90 s | 95% | **100%** (p95 74 s) | 100% | 100% |
| investigation cost ≤ $0.40 | 95% | **100%** (p95 $0.35) | (scripted) | (scripted) |
| runs ending in a decision for a human | 99% | 100% | 100% | 100% |
| tool calls within 200 ms | 95% | 100% (p95 27 ms) | 100% | 100% |
| ingest durable within 2 s at 500 events/s | 99% | (n/a) | 100% (p99 25 ms) | **98.4%, missed** |
| valid events stored, not dead-lettered | 99.9% | (n/a) | 100% | **99.5%, missed** |
| case API within 500 ms | 95% | (n/a) | 100% | 100% |

The fault run misses on purpose. A 5% transient-failure rate and injected
poison messages burn the ingest error budgets, so the SLOs catch a degraded
dependency even though no event is lost.

### Total model spend for the project

About **$6.68**. Everything else runs on the free scripted model.

## What I'd flag in review

The full list is in [DECISIONS.md](docs/DECISIONS.md), with 49 decisions and
28 mistakes, each with a root cause. Some highlights:
- **M-25.** The budget's "a call can never overshoot the cap" guarantee was
  false on real traffic.
  - The input-token estimate was 1.4–1.8× too low on UUID-heavy JSON.
  - The free scripted model hid it.
  - The UI made it visible.
  - It's fixed and replayed offline against the stored runs.
- **M-24.** The guardrails were tuned against a scripted attacker and wrongly
  escalated 3 of 5 *correct* Claude reports. Guardrails need false-positive
  measurement on real model prose.
- **M-17 and M-18.** The citation validator flagged correct reasoning about
  thresholds that were *not* met. Re-scoring all 153 drafts offline cost $0.

## Known gaps

- **Infrastructure.** SQLite and a single process. There's no Docker here, so
  no Redpanda or Postgres, but the `Broker` and store interfaces are where
  they'd plug in.
- **Data.** Synthetic, self-labelled data, and few real-model samples
  (cost-capped), so the confidence intervals are wide.
- **Review flow.** Approve/reject isn't in the UI. A browser write path needs
  real reviewer identity (SSO) and CSRF protection, which a pasted bearer token
  isn't.
- **Credentials.** There's one shared HMAC secret across services, where
  asymmetric signing would be the fix. `/metrics` is unauthenticated (bind it
  to localhost).
- **Telemetry.** Dependency-free, using standard formats (Prometheus text, W3C
  `traceparent`). OpenTelemetry would replace the exporter, not the call sites.

## Layout

```
driveguard/
  sim/          seeded simulator + fault injector
  pipeline/     broker, consumer, detector
  store/        SQLite store, migrations, read queries
  mcp_servers/  trips, risk, cases servers; JWT auth; error envelope
  agents/       orchestrator, specialists, gateway, budget, trace, citation validator
  guardrails/   spotlighting, PII redaction, output guard
  evals/        datasets, metrics, runner, regression gate
  redteam/      attack suite
  obs/          logs, metrics, tracing, SLOs, benchmark
  api/          read-only reviewer API
web/            React + TypeScript reviewer UI (Vite, Vitest)
tests/          pytest suites (270)
docs/           design, decisions, evals, security, operations
```

## License

MIT. See [LICENSE](LICENSE).
