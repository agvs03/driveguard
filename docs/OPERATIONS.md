# DriveGuard operations: logs, metrics, tracing, SLOs

Code: [`driveguard/obs/`](../driveguard/obs). Decisions: D-45 to D-47 in
[DECISIONS.md](DECISIONS.md).

## Three signals, one join key

| Signal | Where | Holds | Never holds |
|---|---|---|---|
| **Logs** | stderr, one JSON object per line (`--log-format text` for humans) | stable `event` names plus fields; trace, run and case ids from context | payloads, note text, prompts, credentials (redacted on the way out) |
| **Metrics** | `/metrics` on the API (Prometheus text); in-process registry | counters and histograms with small closed-set labels | ids or free text as label values |
| **Spans** | `--trace-file spans.jsonl` (JSONL exporter), DEBUG logs | timing tree across orchestrator → agents → model calls → tool calls → MCP handlers | payloads |
| **Audit trace** (Phase 3) | `audit_trace` table, hash-chained | every model call, tool result, check and decision of a run | credentials |

**The join key: an investigation's `trace_id` equals its `run_id`** (the
UUID's hex). A slow `investigation` span or an `investigation_finished` log
line leads straight to the audit trace and to the case in the UI.

```
investigation (orchestrator)            trace_id = run_id
├── tool_call cases__create_case
│   └── mcp_tool cases/create_case       server side: in-process via contextvars,
├── agent triage                           over HTTP via the traceparent header
│   ├── llm_call
│   ├── tool_call trips__list_events
│   │   └── mcp_tool trips/list_events
│   └── llm_call
├── agent evidence ...
└── agent report_writer
    └── llm_call
```

Both propagation paths are tested:
- `test_one_investigation_is_one_trace_from_orchestrator_into_mcp_handlers`
  (in process);
- `test_investigation_over_http` (real uvicorn servers).

### Example log lines (real output of `driveguard -v investigate`, scripted model)

```json
{"ts":"2026-11-06T03:58:23.780Z","level":"info","logger":"driveguard.mcp","event":"mcp_tool_call","run_id":"f3607d24-5836-4046-b0b3-1bc87eaea4e9","incident_id":"9b63a202-9152-5316-9db7-4d0ee32cc27a","component":"mcp:cases","trace_id":"f3607d2458364046b0b31bc87eaea4e9","span_id":"edea0a40090786ff","case_id":"493d9e28-1f02-521d-81e1-7b44fc300bb6","server":"cases","tool":"start_investigation","code":"OK","sub":"orchestrator:f3607d24","role":"agent","latency_ms":0.66}
{"ts":"2026-11-06T03:58:24.029Z","level":"info","logger":"driveguard.orchestrator","event":"investigation_finished","run_id":"f3607d24-5836-4046-b0b3-1bc87eaea4e9","incident_id":"9b63a202-9152-5316-9db7-4d0ee32cc27a","component":"orchestrator","trace_id":"f3607d2458364046b0b31bc87eaea4e9","span_id":"5021ba75d61916eb","case_id":"493d9e28-1f02-521d-81e1-7b44fc300bb6","status":"awaiting_approval","cause":null,"cost_usd":0.0,"latency_s":0.428,"model_calls":8,"tool_calls":5}
```

## Metric catalogue

All metrics are declared in one place:
[`obs/metrics.py`](../driveguard/obs/metrics.py).

| Metric | Type | Labels | Feeds SLO |
|---|---|---|---|
| `driveguard_ingest_messages_total` | counter | outcome: inserted, late, duplicate, retry, dead, stale_lease | |
| `driveguard_ingest_latency_seconds` | histogram | | ingest-latency |
| `driveguard_ingest_dead_letters_total` | counter | reason | |
| `driveguard_mcp_tool_calls_total` | counter | server, tool, code | |
| `driveguard_mcp_tool_latency_seconds` | histogram | server, tool | (server side) |
| `driveguard_gateway_tool_latency_seconds` | histogram | tool | (client side) |
| `driveguard_llm_calls_total` | counter | agent, model, outcome (stop reason or error kind) | |
| `driveguard_llm_latency_seconds` | histogram | agent | |
| `driveguard_llm_tokens_total` | counter | agent, kind: input, output, cache_read, cache_write | |
| `driveguard_llm_cost_usd_total` | counter | agent | |
| `driveguard_investigations_total` | counter | status | |
| `driveguard_investigation_latency_seconds` | histogram | | investigation-latency |
| `driveguard_investigation_cost_usd` | histogram | | investigation-cost |
| `driveguard_escalations_total` | counter | cause: output_guard, citation_validation, denials, budget, llm_error, agent_failure, internal, other | |
| `driveguard_guardrail_events_total` | counter | guard: untrusted_text, output_guard, tool_allowlist, argument_limits, denial_abort | |
| `driveguard_api_requests_total` | counter | route (template), status | |
| `driveguard_api_latency_seconds` | histogram | route | api-case-load |

Escalation causes are mapped from free text to a closed set
(`escalation_cause`), because a label must never be free text.

## SLOs

Each SLO is a ratio of good events to valid events, and the error budget is
`1 − objective`. Definitions with their reasons are in
[`obs/slo.py`](../driveguard/obs/slo.py).

| SLO | SLI (good / valid) | Objective | Why this number |
|---|---|---|---|
| ingest-latency | events durable within 2 s of publish | 99% | the detector assumes near-real-time data; 500 events/s locally |
| ingest-availability | valid messages stored without exhausting retries (schema/conflict = producer errors, excluded) | 99.9% | a dead-lettered valid event is a missed incident until someone replays the DLQ |
| mcp-tool-latency | agent tool calls answered within 200 ms, client-observed | 95% | ~10 calls per case; keeps tools under 5% of the 90 s budget |
| mcp-tool-availability | tool calls without INTERNAL/UNAVAILABLE | 99.9% | a server fault escalates the case, which costs a human review |
| investigation-latency | runs finished within 90 s (excluding human wait) | 95% | the case should be ready before a reviewer reaches it in the queue |
| investigation-completion | runs ending awaiting_approval or escalated (failed or stuck = bad) | 99% | a failed run leaves a case nobody is looking at |
| investigation-cost | runs costing at most $0.40 (hard cap $0.50) | 95% | catches cost drift before the cap starts escalating cases |
| api-case-load | case-detail responses within 500 ms | 95% | the reviewer's main page |

```bash
driveguard slo --db "data/evals/runs/<run-id>/*.db"   # stored runs, e.g. the real-Claude eval
driveguard slo --bench                                # local load: ingest at 500/s, agents, API
driveguard slo --bench --faults                       # same, with the Phase 1 fault profile injected
```

### Results

Measured on 2026-11-05 on one Windows laptop with SQLite.

#### Real Claude Opus 5.5: mini eval run (2 samples, 19 investigations)

| SLO | objective | good / total | compliance | error budget used | p50 | p95 | p99 | status |
|---|---|---|---|---|---|---|---|---|
| ingest-latency | 99.0% | 0 / 0 | - | - | - | - | - | no data |
| ingest-availability | 99.9% | 0 / 0 | - | - | - | - | - | no data |
| mcp-tool-latency | 95.0% | 243 / 243 | 100.00% | 0% | 13 ms | 27 ms | 60 ms | met |
| mcp-tool-availability | 99.9% | 243 / 243 | 100.00% | 0% | - | - | - | met |
| investigation-latency | 95.0% | 19 / 19 | 100.00% | 0% | 57.6 s | 74.1 s | 74.1 s | met |
| investigation-completion | 99.0% | 19 / 19 | 100.00% | 0% | - | - | - | met |
| investigation-cost | 95.0% | 19 / 19 | 100.00% | 0% | $0.240 | $0.348 | $0.348 | met |
| api-case-load | 95.0% | 0 / 0 | - | - | - | - | - | no data |

- **ingest-latency**: needs a running process: use --bench
- **investigation-completion**: {"awaiting_approval": 19}
- **api-case-load**: needs a running process: use --bench


Every SLO that this data can measure is met: investigation p95 is **74 s against 90 s**. (Percentiles here
are nearest-rank; EVALS.md interpolates, so it reports 73 s for the same runs.)
and cost p95 is $0.35 against $0.40.

- **Small sample.** With 19 runs, a 95% objective can't absorb even one slow
  run: 18 of 19 is 94.7%.
- **Close to the cost line.** The most expensive run, $0.348, sits $0.05 under
  the $0.40 line. That's the first SLO that would burn if contexts grow.

#### SLO report: local benchmark (500 events/s, scripted model)

| SLO | objective | good / total | compliance | error budget used | p50 | p95 | p99 | status |
|---|---|---|---|---|---|---|---|---|
| ingest-latency | 99.0% | 10000 / 10000 | 100.00% | 0% | 10 ms | 20 ms | 25 ms | met |
| ingest-availability | 99.9% | 10000 / 10000 | 100.00% | 0% | - | - | - | met |
| mcp-tool-latency | 95.0% | 117 / 117 | 100.00% | 0% | 13 ms | 39 ms | 42 ms | met |
| mcp-tool-availability | 99.9% | 117 / 117 | 100.00% | 0% | - | - | - | met |
| investigation-latency | 95.0% | 9 / 9 | 100.00% | 0% | 312 ms | 402 ms | 402 ms | met |
| investigation-completion | 99.0% | 9 / 9 | 100.00% | 0% | - | - | - | met |
| investigation-cost | 95.0% | 9 / 9 | 100.00% | 0% | $0.000 | $0.000 | $0.000 | met |
| api-case-load | 95.0% | 300 / 300 | 100.00% | 0% | 1 ms | 1 ms | 1 ms | met |

- **investigation-completion**: {"awaiting_approval": 9}


Clean path: 10,000 events published at 500 events/s and drained in 20.0 s
(the publish window), so the consumer keeps up. Ingest p99 is 25 ms against
2 s.

The API figure is server-side only. In the browser, a full case page from the
real-Claude data loaded as follows (one sample, same machine):
- the case JSON (10 kB) arrived in 5 ms;
- the trace (110 kB, 31 steps) in 18 ms;
- DOMContentLoaded at 265 ms.

#### SLO report: local benchmark (500 events/s, scripted model, ingestion faults injected)

| SLO | objective | good / total | compliance | error budget used | p50 | p95 | p99 | status |
|---|---|---|---|---|---|---|---|---|
| ingest-latency | 99.0% | 9844 / 10000 | 98.44% | 156% | 11 ms | 95 ms | 2.8 s | **MISSED** |
| ingest-availability | 99.9% | 10906 / 10956 | 99.54% | 456% | - | - | - | **MISSED** |
| mcp-tool-latency | 95.0% | 104 / 104 | 100.00% | 0% | 13 ms | 41 ms | 55 ms | met |
| mcp-tool-availability | 99.9% | 104 / 104 | 100.00% | 0% | - | - | - | met |
| investigation-latency | 95.0% | 8 / 8 | 100.00% | 0% | 339 ms | 421 ms | 421 ms | met |
| investigation-completion | 99.0% | 8 / 8 | 100.00% | 0% | - | - | - | met |
| investigation-cost | 95.0% | 8 / 8 | 100.00% | 0% | $0.000 | $0.000 | $0.000 | met |
| api-case-load | 95.0% | 300 / 300 | 100.00% | 0% | 1 ms | 1 ms | 1 ms | met |

- **ingest-availability**: excluded producer errors: {'schema': 97}
- **investigation-completion**: {"awaiting_approval": 8}


**This is the run that shows the SLOs working.** With Phase 1's fault profile
injected, the consumer handled:
- 1,375 retries;
- 1,035 duplicates;
- 97 corrupt bodies;
- 50 poison messages.

It lost nothing, yet two SLOs miss:
- **Ingest latency, 98.44% (156% of budget).** Events that fail two or more
  times wait out exponential backoff (0.5 s base, full jitter), so about 1.5%
  take over 2 s. The 5% transient-failure rate is deliberately about 10× worse
  than a healthy store. In production this burn rate would page, and the
  first check is `driveguard_ingest_messages_total{outcome="retry"}`.
- **Ingest availability, 99.54% (456% of budget).** The 0.4% injected poison
  rate (`retries_exhausted` plus `permanent`) is four times the 0.1% budget.
  The 97 corrupt bodies are producer errors, so they're excluded, as designed.

Policy and objective were left unchanged: the bench exists to show that the
SLOs detect a degraded dependency, not to tune them to pass.

**What these numbers are not:**
- The bench uses the free scripted model, so investigation latency and cost
  there measure our orchestration, not Claude.
- Real-model latency and cost come only from the stored eval run above.

## Alerting (how these would page, not built here)

Use multi-window burn-rate alerts on each SLO's error budget, as in the SRE
workbook:
- **Page** when the 1 h and 5 min burn rates both exceed 14.4× (2% of a 30-day
  budget gone in an hour).
- **Ticket** when the 6 h and 30 min burn rates both exceed 6×.

Low-traffic SLOs (investigations: tens per day) need a minimum event count
before alerting, or one slow run pages someone.

| Symptom | First look | Likely cause |
|---|---|---|
| ingest-latency burning | `driveguard_ingest_messages_total{outcome="retry"}` rising | store errors and retries with backoff (see the fault bench) |
| ingest-availability burning | `driveguard dlq list` | retries_exhausted: replay after fixing; permanent: a bug |
| investigation-latency burning | `driveguard_llm_latency_seconds` by agent; spans for the slow run | model latency, or a loop near the call limits |
| escalations up | `driveguard_escalations_total` by cause | `output_guard` means a model or prompt change, or an attack (check `guardrail_events_total{guard="untrusted_text"}`); `budget` means cost drift |
| investigation-cost burning | `driveguard_llm_tokens_total` by agent and kind | cache misses (`cache_read` falling) or longer contexts |
