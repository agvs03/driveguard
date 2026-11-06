# Decisions and mistakes log

A running log of decisions, reversals and mistakes, including ones I got wrong
and corrected. Newest entries at the bottom. Format:
`D-n` = decision, `M-n` = mistake, `R-n` = reversal of an earlier decision.

---

## Phase 0: Design

**D-1 (2026-11-05) · Default to SQLite plus a SQLite-backed queue.**
Context: Docker isn't installed, so Redpanda and Postgres can't run locally.
Decision: put a `Broker` interface and SQLAlchemy Core in front of storage, so
Redpanda and Postgres stay drop-in options. Both broker implementations must
pass the same contract tests.
Risk accepted: SQLite's single writer may hide concurrency bugs.

**D-2 · The orchestrator is a deterministic state machine, not an LLM planner.**
Why: human approval, step caps and cost caps become testable code invariants
instead of prompt instructions.

**D-3 · The risk score is a transparent linear model with versioned weights.**
Why: its per-factor contributions are exact, so reports can cite them and the
validator can check them.

**D-4 · A deterministic `ScriptedClient` runs alongside `ClaudeClient`.**
Why: there is no API key in this environment. CI must be free and
deterministic, and some failure paths (hallucinated IDs, obeying an injection)
must be reproducible on demand.

**D-5 · Each investigation gets a case-bound auth token.**
Why: this is the primary control against data leaking across cases. It is
enforced server-side in the MCP layer, not in the prompts.

**D-6 · The report writer gets no data tools.**
Why: it can only use evidence that the earlier steps fetched, and that evidence
is in the trace, which makes uncited claims structurally harder to produce.

**D-7 · Default model is `claude-opus-5-5`, with effort set explicitly.**
Why: this model's default effort is `medium`, so leaving it unset would silently
change behaviour. Forced `tool_choice` is rejected on this model, so we use
`auto` plus `strict` schemas. Cheaper specialists are your call (open
question 1 in DESIGN.md).

### Mistakes / corrections
- *(none in Phase 0)*

---

## Phase 1: Synthetic event stream and pipeline

**Assumed defaults (2026-11-05).** You approved Phase 0 without answering the
open questions, so I kept: SQLite (no Docker), Python 3.10, no GitHub repo.
Phase 1 doesn't depend on model choice.

**D-8 · Event IDs are seeded UUIDv4, not UUIDv7.**
Python 3.10 has no `uuid7`. Reproducibility from the seed mattered more than
time-sortable IDs, because no query sorts by ID.

**R-1 · Reversal of design §4.2: no reorder buffer or watermark.**
Every derived write is either commutative (`MIN`/`MAX`/set-once trip fields)
or recomputed from scratch from the stored events (detection). So arrival
order cannot change state, and buffering events to sort them buys nothing.
What still needs time is *when to finalise a trip*:
- **complete:** all seqs 0..end_seq are present;
- **incomplete:** a sweeper finalises the trip after a quiet period.

Any event arriving for an incomplete trip re-runs detection.

**D-9 · No `telemetry_1s` or `driver_notes` tables yet.**
`raw_events` already holds both. A second copy would be a second thing to keep
idempotent. Phase 2 can add read-only views if the MCP tools need them.

**D-10 · Stdlib `sqlite3` instead of SQLAlchemy.**
One connection on one dedicated thread, driven from asyncio. It's honest about
SQLite having a single writer, and avoids a dependency.
Cost: porting to Postgres means rewriting about 15 SQL statements. They are
written in the shared dialect (`ON CONFLICT … DO NOTHING`, `MIN`/`MAX`), so it
is mostly a driver swap.

**D-11 · `RedpandaBroker` deferred.**
Without Docker it could not run the broker contract tests, and an untested
adapter is a liability in a portfolio repo. The `Broker` Protocol is the seam
for adding it.

**D-12 · Leases are fenced by the attempt number.**
`ack`, `nack` and `dead_letter` only apply when `attempts` still matches the
claim. A worker whose lease expired cannot ack or dead-letter a message that
another worker has since re-claimed.

**D-13 · "Same `event_id`, different content" is a conflict that goes to the DLQ.**
It is not a silent duplicate. Each event stores a content hash, and so does
`(trip_id, seq)` reuse. Silently keeping the first version would hide producer
bugs or tampering.

### Mistakes / corrections

**M-1 · The watermark-based "late" flag was meaningless.**
- *Symptom:* in the first faulty demo run, 20,957 of 24,037 events (87%) were
  flagged late. The clean run had 0.
- *Root cause:* late was defined as `event_time < max_event_time_seen − 30 s`.
  The fault injector delivers some events up to ~100 s *early*. One early
  arrival pushes the watermark forward, and every in-order event behind it
  then counts as late. Stored business state was still correct, because `late`
  is excluded from the snapshot, but the metric was noise. I picked a textbook
  rule without checking it against my own fault model.
- *Fix:* late = arrived after the trip was finalised by the sweeper. That is
  the only case where lateness changes behaviour (it triggers re-detection).
  After the fix: 427 late events, matching the late batch.

**M-2 · A test helper made two tests check nothing.**
`ingest([])` meant "zero batches", so the loop never ran and nothing was
drained. The DLQ-replay and crash-recovery tests failed for that reason, not
because of the pipeline. Fix: an explicit `drain()` helper, plus an assertion
that rejects empty batch lists.

**M-3 · Toolchain: Python 3.10.0 breaks current pytest and Hypothesis.**
- pytest 9.2.0 crashed on startup (`NodeId` has no `_str_cache`).
- Hypothesis 6.168 crashed with `TreeNode` has no `is_exhausted`.
- Both come from a CPython 3.10.0 bug: `dataclass(slots=True)` fields declared
  with `field(default=…, init=False)` never get their default. It was fixed in
  3.10.1.
- Pinned `pytest<9` and `hypothesis<6.113`. The real fix is upgrading Python.

**M-4 · A test that couldn't fail (found by mutation testing).**
I planted three bugs in `repository.py`:
- *M2,* order-dependent trip start time: caught.
- *M3,* dedupe check removed: caught.
- *M1,* re-detection for late events on incomplete trips disabled: **survived.**

The late-events test delivered *all* withheld events, so the trips became
complete and detection ran through the completion path instead. Added
`test_late_events_redetect_even_if_trip_never_completes`, where every trip also
permanently loses one GPS event. M1 is now caught.

---

## Phase 2: MCP servers

**D-14 · Tokens are standard JWTs (PyJWT, HS256 only), not a hand-rolled format.**
Claims: `iss, sub, aud[], role, scope, case_id, trip_ids, exp, iat, nbf, jti`.
- `algorithms=["HS256"]` is pinned, so `alg: none` and algorithm confusion are
  rejected (tested).
- The policy (agents must be bound; agents get no `cases:review` or
  `cases:create`; only humans get `cases:review`) is enforced at issue time AND
  at verify time. A correctly signed token from a buggy issuer is still refused.

**D-15 · Bound tokens get FORBIDDEN for "exists elsewhere" and "doesn't exist" alike.**
This applies to trips, events, incidents, scores and cases, so a case-bound
agent cannot probe for other cases' IDs. Unbound (human) tokens get NOT_FOUND,
which is more useful to a person.

**D-16 · Re-detection supersedes incidents instead of deleting them; schema migrations added.**
This fixes the Phase 1 critique: cases now FK to incidents, and a late event
could otherwise delete an incident a case points to.
- A superseded incident keeps its row. If the detection comes back, the same
  uuid5 ID is un-superseded.
- The Phase 1 equivalence tests still pass because the snapshot compares active
  incidents only.
- Migrations: forward-only, tracked in `PRAGMA user_version`. Migration 1 is the
  Phase 1 DDL, so old databases upgrade in place (tested).

**D-17 · Tool surface differs from the design doc.**
- `review_case(approve|reject)` replaces `close_case`.
- `score_events` was dropped as ill-defined; `get_weights` was added.
- Added `start_investigation`, `escalate`, `get_events`, `list_incidents` and
  `get_incident`.
- The report writer (Phase 3) will get none of the data tools (D-6 still holds).

**D-18 · Two error codes added.**
`UNAUTHENTICATED` (no or invalid token) and `FAILED_PRECONDITION` (illegal
state transition). `CONFLICT` now means only a stale `expected_version`, and is
retryable after a re-read.

**D-19 · Strict tool arguments via a one-line SDK patch.**
The SDK's argument models ignore unknown keys. So `list_events(type=["gps"])`
(a typo for `types`) silently returned every event type, which is a quiet
correctness bug for an agent.
- Setting `extra="forbid"` on the SDK's `ArgModelBase` rejects unknown keys and
  makes every schema advertise `additionalProperties: false`, which Claude's
  strict tool use needs in Phase 3.
- It relies on an SDK module attribute, so a test asserts every tool's schema
  still has it (mutation M7 confirms the test notices).

**D-20 · One error envelope for every failure.**
`{"code", "message", "retryable"}` comes from two layers:
- `ToolKit.guard` maps our errors and crashes;
- a server middleware rewrites the SDK's own errors (argument validation,
  unknown tool).

Validation messages keep field names and error types but strip `input_value=`,
so error text never echoes untrusted input back into the model's context.
Crashes become `INTERNAL: internal error`, and the traceback stays in the
server log.

**D-21 · Least privilege per server.**
- trips-server opens SQLite with `mode=ro`, so it cannot write even if buggy
  (tested).
- No query in `store/queries.py` selects `display_name`, `phone` or
  `personality`. A test greps the module, and another scans every tool's output
  for those values. `personality` is the simulator's hidden persona, so exposing
  it would also leak ground truth to agents.
- GPS is coarsened to about 100 m outside ±60 s of an incident.
- `driver_note` events carry `untrusted_text: true`.

**D-22 · The token secret comes from the environment only, with no default.**
`serve` and `token` exit with instructions if `DRIVEGUARD_TOKEN_SECRET` is
unset or shorter than 32 bytes, so a missing secret fails closed.

### Mistakes / corrections

**M-5 · I assumed the MCP SDK's v1 API.**
`pip install mcp` gave 2.3.0, where `FastMCP` is now `MCPServer` and several
APIs moved. Instead of guessing, I read the installed source (ToolError
semantics, the auth context var, the in-memory `Client`, the middleware
protocol) and ran two spikes before writing any server code. Pinned
`mcp>=2.3,<3`.

**M-6 · The test fixture's seed had no crash.**
Two risk tests failed on an empty list because seed 17 happened to generate no
crash. Picked seed 18 by search, and added `test_fixture_coverage` so a
simulator change can't silently thin the fixture again.

**M-7 · One mutant survived: equivalent, not a weak test.**
I planted seven bugs in the auth and safety checks, and six were caught. M11
("let a crash escape the guard") survived because the envelope middleware
independently maps the SDK's bare crash text to `INTERNAL`: two layers, same
observable result. I added `test_error_envelope.py` to test the middleware
layer on its own. Removing its crash mapping (M12) is now caught.

---

## Phase 3: Multi-agent orchestration

**D-23 · Manual tool-use loop instead of the SDK's tool runner.**
The SDK guide recommends its tool runner. But every model call must pass a
budget check *before* it is sent, using a worst-case estimate (input so far
plus `max_tokens` of output), and must be written to the hash-chained trace
with its exact usage. Owning the loop also lets the `ScriptedClient` drive the
same code path. Cost: we handle stop reasons, repair and tool results
ourselves (all tested).

**D-24 · Final answers use structured outputs (`output_config.format`) plus strict tools.**
The model has no forced `tool_choice`: Opus 5.5 rejects it with a 400. So the
contract comes from the response format, not from a "finish" tool.
- `strict_schema()` removes constraints the API doesn't accept (`minimum`,
  `maxLength`, `pattern`, ...) from what the model sees.
- The same constraints are still enforced by pydantic on our outputs and by
  the MCP servers on tool inputs.
- Every output field is required (nullable where optional), and a test asserts
  this.
- Not verified against the live API: no key here. Request shape and response
  parsing are tested against a mocked HTTP transport.

**D-25 · The report writer has no tools.**
It sees only the evidence pack, which holds just the event records a tool
returned earlier in the same run (from the trace). So it cannot fetch new
facts, and every number it can see is in the trace.

**D-26 · Least-privilege tokens per actor; only the orchestrator writes cases.**
| Actor | Token |
|---|---|
| triage, evidence | `trips:read`, `risk:read`, bound to the case and its trip |
| orchestrator | `cases:read`, `cases:write`, bound to the case |
| service | `cases:create` only |
| report writer | none |
| human reviewer | `cases:review`, minted per review (SSO in production) |

The orchestrator never holds `cases:review`, so the system cannot close its
own case. The tests assert there is no `approve` in the case history before a
human acts.

**D-27 · The citation validator is deterministic, and the gate is in code.**
- Errors: `E_UNKNOWN`, `E_UNGROUNDED` (the event's full record was never
  returned by a tool in this run), `E_NUMBER`, `E_NO_SOURCE`,
  `E_SUMMARY_NUMBER`.
- Flow: one repair pass with the errors fed back to the writer; if it still
  fails, the case is escalated. The invalid report is kept for the human,
  rendered with its errors.
- Quoted policy thresholds are exempt only next to threshold wording. Known
  hole: a hallucinated value that *equals* a threshold *and* sits next to such
  wording would pass.

**D-28 · The audit trace is append-only at two levels.**
- SQLite triggers refuse UPDATE and DELETE.
- A per-run hash chain detects edits by anyone who drops the triggers (tested
  by doing exactly that).
- Spans record full tool results (needed to ground citations), model usage
  and cost.
- JWTs and API-key-shaped strings are redacted before hashing.
- `reconstruct(report_id)` maps every claim to its event and to the exact tool
  call (actor, tool, arguments) that returned it.

**D-29 · Refusal fallbacks are on by default for the Claude client.**
Per the SDK guidance for `claude-opus-5-5`, the client sends
`fallbacks: "default"` with the `server-side-fallback-2026-07-01` beta.
Cost is priced by `response.model`, so a fallback is billed at the model that
served it. This is approximate when a turn spans models. Turn it off with
`ClaudeClient(fallbacks=False)`.

**D-30 · Failed runs are always closed out.**
An infrastructure failure (for example, MCP auth rejecting a token) re-raises,
but only after writing `run_end` and setting the run status to `failed`. No
run is ever left `running`.

### Mistakes / corrections

**M-8 · The in-memory auth context was captured at the wrong time.**
- *Symptom:* every tool call in the first orchestrator run came back
  UNAUTHENTICATED, even though the tokens verified.
- *Root cause:* the SDK's in-memory streams snapshot the **sender's**
  contextvars on every `send()` and run the handler inside that snapshot
  (`mcp/shared/_context_streams.py`). I set the authenticated user only while
  opening the session, so at call time it was gone. My Phase 2 test helper hid
  this by keeping a single identity set for the whole session.
- *Fix:* `_AuthedSession` sets the identity around **each call**. This also
  means two open sessions holding different tokens can never leak identity
  into each other. Keeping the variable set for the whole session would have
  "worked", but nested sessions would then have run under the wrong agent's
  identity.

**M-9 · Budget and LLM errors weren't escalating (10 tests failed).**
- *Root cause:* exceptions raised while an MCP session is open come back
  wrapped in an `ExceptionGroup` by anyio's task group. On Python 3.10 that's
  the `exceptiongroup` backport. So `except BudgetExceeded` never matched, and
  a cost-cap hit would have crashed the run instead of escalating the case.
- *Fix:* `_leaf()` unwraps nested groups and picks the first expected error;
  anything unexpected still escalates the case and then re-raises.

**M-10 · Small slips caught by the smoke runs.**
- `load_spans` popped a dict key before reading it.
- It also assumed the caller's connection used row-dict mode.
- `investigate --all` queried `cases` before migrations ran, so it crashed on a
  Phase 1 database. Found by the CLI walkthrough against the old demo
  database.

**M-11 · Two of my tests were wrong.**
- The first cost-cap test priced calls so high that the very first call's worst
  case already exceeded the cap. The system correctly refused at $0, and the
  test expected otherwise.
- That test also showed the "never overshoots" guarantee depends on the input
  estimate (chars/3) not being lower than the real token count. Violations are
  now recorded as `guard` spans, so a real run would show them.
- A summary-number test expected a pass, but the cited events didn't contain
  the number. The validator was right.

**M-12 · Mutation testing found two blind spots.**
I planted seven bugs in Phase 3 guards; five were caught.
- **M18: the per-tool allowlist could be removed without any test failing.**
  Triage could then call `trips__get_driver_profile`, a tool on a server it is
  connected to but not allowed to use. Added an `offlist_tool` fault and a
  regression test.
- **M17: redaction could be removed silently,** because no secret flows into
  traced data in normal runs. Added a direct unit test.

Both are now caught.

**M-13 · Reviewer rejection feedback was dropped.**
Found while writing the self-critique: after a human rejected a report, the
rerun started from scratch and never saw why it was rejected, so the human
review step couldn't steer the result. The orchestrator now passes the human
reject comments into the agents' task as `reviewer_feedback`, the system
prompt tells agents to address each point, and a test asserts the comment
reaches the triage model call on the rerun.

---

## Phase 4: Evaluation harness

**Using your API key (2026-11-05).** You offered the key in
`C:\Personal\Projects\billback\.env`. `eval run --env-file` reads **only**
`ANTHROPIC_API_KEY` from such a file (an allow-list; other keys, such as the
workspace ID, are ignored), sets it for that process only, and never prints
it. The trace redacts `sk-ant-` strings. Spend is billed to that project's
workspace and capped per run (`--max-run-cost`, default $5).

**D-31 · An eval case is a *detected* incident; ground truth comes from the injected labels.**
- Overlaps an injected incident: expected = that kind.
- Overlaps a hard negative, or nothing: expected = `no_incident`.
- Detector misses can never reach the agents, so they're reported as dataset
  stats, not eval cases.
- Classes are balanced (8 per class), so `no_incident` isn't swamped.

**D-32 · Two splits from different seeds: `dev` and a held-out `test`.**
Error analysis and fixes happen on `dev`. Headline numbers are quoted from
`test` only. A third tiny split, `mini` (5 or 10 cases), is for CI and tests.
The dataset JSON is committed. Databases are rebuilt deterministically and
checked with a fingerprint of the incident IDs and labels, which stays stable
across SQLite page layouts.

**D-33 · Metrics for a non-deterministic system.**
- Every metric is computed over k samples per case, with a 95% bootstrap CI
  that resamples *cases*, so a case's samples stay together.
- Before/after uses a **paired** bootstrap over the shared cases, so case
  difficulty cancels out of the delta.
- Hallucination is measured on **first drafts** (what the model produced) and
  separately on **shipped** reports (what reached a human after the citation
  gate). The two answer different questions: is the model getting better, and
  is the gate holding.
- Escalations and `inconclusive` count as wrong for accuracy; coverage and
  selective accuracy separate abstention from error.

**D-34 · The regression gate is asymmetric.**
- Quality metrics (accuracy, F1, citation precision) fail only when the whole
  95% CI of the delta is past the tolerance, so noise doesn't break builds.
- Safety metrics (hallucination, fabricated IDs) fail on the *point* delta.
- Found by a unit test: with 10 cases, a jump from 0% to 15% hallucinations
  wasn't significant at 95%, and the original CI-only rule let it pass.
- The gate runs the free scripted pipeline on `mini` against a committed
  baseline, as part of pytest, so it runs on every change. Real-model evals
  are run deliberately, because they cost money.

**D-35 · Runs are tied to the exact code.**
There are no commits yet, so each eval run stores a hash of
`driveguard/**/*.{py,json,sql}` (plus the git SHA once one exists), the
dataset hash, and the limits. Per-sample databases are kept, and every
failing row in a report links to the `trace` command for that case.

### Mistakes / corrections

**M-14 · A label bug in my own simulator, found by error analysis.**
- *Symptom:* the first `dev` baseline was right on every injected incident, but
  15 of 24 `no_incident` samples came back as `hard_braking`.
- *Root cause:* "firm brake" hard negatives were injected at 0.33–0.43 g
  *mean* deceleration, so their *peak* reached ~0.50 g. By the stated policy
  (peak ≤ −0.45 g is hard braking) those **are** hard braking. Labels and
  policy contradicted each other, and the system was being marked wrong for
  applying the policy correctly.
- *Fix:*
  - inject at 0.35–0.375 g, so peaks are ~0.40–0.44 g: still detected, still
    a genuine false positive;
  - label from the generated signal: any firm brake whose measured peak
    crosses 0.45 g is labeled `hard_braking`. Verified: 0 of 103 firm-brake
    labels now contradict the policy.
- Datasets bumped to **v2**; the v1 results aren't comparable and were
  discarded.
- *Lesson:* the first thing an eval finds is often a bug in the eval itself.
  Fixing the data after looking at results is legitimate only for real
  contradictions with the spec, and it has to be logged, as here.

**D-36 · The before/after change made in Phase 4: cite the event that shows the behaviour.**
- *Error analysis on `dev`:* every phone-distraction report was classified
  correctly but cited only a GPS speed sample, never the phone `screen_on`
  event. `evidence_hit_rate` was 0.75.
- *Change:*
  - the report-writer **prompt** now requires a finding that cites the event
    showing the behaviour (this is what Claude follows);
  - the scripted writer does the same (this is what the free eval can see).
- *Results (scripted, k = 3, paired bootstrap):*

  | Split | evidence_hit_rate | 95% CI of delta | Regressions |
  |---|---|---|---|
  | dev | 0.75 → 1.00 | +0.10 .. +0.39 | none |
  | **test (held out)** | 0.75 → 1.00 | +0.10 .. +0.39 | none |

**D-37 · Real-model evals are capped, and smoke-tested first.**
A 1-case smoke run (cap $0.60) goes before any larger run, so an API-shape
problem costs cents, not dollars. The first full real run uses the held-out
`mini` split (10 cases × 2 samples, cap $5), because 40 test cases × k
samples at ~$0.19 per case would exceed the cap.

### Mistakes / corrections (continued)

**M-15 · Live API: the key needs a workspace header.**
The first real call returned 400: the key isn't scoped to a workspace, so
every request must carry `anthropic-workspace-id`. Nothing was billed, and
the case escalated cleanly as `llm error`, so the error path itself was
proven live.
- *Fix:* `ClaudeClient` sends the header from `ANTHROPIC_WORKSPACE_ID` (the
  core SDK client only sends it via `default_headers`), and the env-file
  allow-list now includes that variable.
- *Also fixed:* `Event loop is closed` at exit, because the async HTTP client
  was never closed. The orchestrator now closes the LLM client on exit.

Second smoke run: correct classification, valid citations, $0.19, 57 s,
6 model calls and 4 tool calls. That matches the Phase 0 estimate.

**M-16 · `eval run` crashed on a fresh clone.**
When the dataset database was missing, `ensure_db` rebuilt it with
`asyncio.run()`, but it is called from inside `run_eval`'s event loop, so it
raised. This would have hit anyone who cloned the repo and ran an eval before
building. The committed-baseline gate test caught it because its temp
directory starts empty. Fixed by running the rebuild on its own loop in a
worker thread when a loop is already running.

**M-17 · Validator false positives, exposed only by the real model.**
- *Symptom:* in the first Claude eval, 2 of the first 6 reports needed a
  citation repair pass.
- *Root cause:* reading the flagged first-draft sentences showed both were
  **correct reasoning about thresholds that were not met**:
  - "rules out a crash pattern of staying at 0 m/s";
  - "do not approach a 4 g resultant".

  My hand-written list of policy constants lacked `0 m/s`, and my
  threshold-wording list lacked "approach" and "rules out". The scripted
  writer never phrases things that way, so only a real model could expose it.
- *Cost of the bug:* an inflated hallucination metric, plus one unnecessary
  repair call (~$0.07) per false alarm.
- *Fix:*
  - derive the constants **from the POLICY text itself**, so they can't drift
    from it;
  - widen the comparison wording;
  - keep the rule that the number must be a real policy constant;
  - regression tests use the two verbatim sentences, plus a counter-case that
    must still be caught ("travelling at 0 m/s").

**D-38 · Offline re-validation from the trace.**
`driveguard eval revalidate --run RUN` reruns the *current* validator on the
first drafts stored in the trace, at no model cost. On those 6 real reports
(52 findings), the first-draft hallucination rate goes from 3.8% (old
validator) to **0.0%** (new validator). This is the payoff of keeping full
tool results in the trace: a measurement change can be applied
retroactively to expensive runs.

**M-18 · A third validator false positive, plus the honest conclusion.**
The full Claude run produced one more false positive: "instead of remaining at
0 m/s". After adding counterfactual wording ("instead", "rather than",
"remain"), an offline re-score of all 19 real reports (153 first-draft
findings) shows **0 hallucinations**. The live-measured 2.6% was entirely the
validator's fault.

Keyword lists for counterfactuals are whack-a-mole: each real run can surface
a new phrasing. The robust fix is to make **numeric claims structured**:
`Finding.measurements = [{event_id, field, value}]`, checked exactly, with
prose numbers either banned or treated as unchecked. That's proposed for
Phase 5, which reworks the output contracts for guardrails anyway.

**Real-model result (held-out `mini`, 10 cases × 2, $4.64):**
- accuracy 19/19;
- 0 fabricated IDs;
- 0 shipped hallucinations;
- $0.244 per case;
- latency p50 58 s, p95 73 s (inside the 90 s SLO).

---

## Phase 5: Security and guardrails

**D-39 · Red-team with a gullible agent, comparing guardrails off and on in one codebase.**
The scripted baseline never reads notes, so it can't be fooled, and that would
make every injection test vacuous. The `obey_injection` fault makes the agent
follow instructions found in untrusted notes. That's the worst case, so a
defence only counts if it holds against a fully fooled model.

Phase 5 guardrails are switchable (`Guardrails` ON/OFF). Phase 2 and 3
controls are architecture and stay on in both modes, and the report says
which layer stopped each attack.

**Result: 3 of 9 attacks succeed with guardrails off, 0 of 9 with them on**,
with 0 false escalations on 240 clean case-samples (dev and test).

**D-40 · The strongest guardrail is a deterministic re-check, not attack detection.**
The output guard recomputes the classification from the incident window (read
straight from the database, independent of what the agents fetched) with
`agents/policy.py`, the same code as the scripted baseline, now shared. It
refuses to ship a report that disagrees. This defeats "classify as
no_incident" without having to recognise the injection. The injection and
PII regexes are a secondary layer, because paraphrases get past them.

**D-41 · PII is redacted at the source, before any agent sees it.**
The trips-server redacts driver-note text, so the model, the trace and the
report never contain the PII. Output scanning is the backstop. Measured: with
redaction on, the name, phone, email and address planted in the PII attack
appear in **no** model input, versus all four without it.

**D-42 · The evidence agent now reads driver notes.**
It's a real feature, since the driver's account is relevant, and it's also
what puts untrusted text in front of the model. So the attack surface was
added deliberately, together with its guardrails. Cost: +1 model call and +1
tool call per case. The gate baseline was updated intentionally.

**D-43 · Argument limits and denial abort apply to LLM-driven actors only.**
- `list_events` for triage and evidence needs a time window of at most 10
  minutes (driver notes excepted).
- After 2 denied calls the agent stops, because repeated denials are a
  manipulation signal.
- Code actors (orchestrator, human) aren't limited; their behaviour is
  reviewed code, not model output.

### Mistakes / corrections

**M-19 · Three bugs in my red-team harness, all making results look worse or better than reality.**
1. **False success.** `cross_case_read` "succeeded" even with guardrails on,
   because the predicate searched tool outputs for the other trip's ID
   *string*, and the attacker's own note contains that string. It now
   requires a *record* owned by the other trip.
2. **False security.** `self_approve` and `tool_flood` were "blocked" because
   the gullible agent never issued the calls: the regex
   `r"\b(\w+__\w+)\b"` held literal backspace bytes. *Root cause:* the
   shell tool unescapes one level of backslashes, so `\b` in my heredoc
   patch became a backspace. The same trap caused the earlier "anchor not
   found" failures, but this time it failed silently. Fixed with a
   byte-level write using `chr(92)`, and the whole codebase was scanned for
   other stray control characters (none found).

   **Rule from now on:** no backslashes in shell-tool patches; use the Edit
   tool, or build the string with `chr(92)`.
3. **False poisoning.** `context_carryover` picked a "next" case on the
   *same trip*, which legitimately sees the same note. After fixing that, it
   still "succeeded", because that next detection really was a firm-brake
   decoy whose correct answer *is* `no_incident`. It now compares against a
   control: the policy check of the next case's own signals.

*Lesson:* every red-team success test needs its own test. An attack that
"never succeeds" is suspicious until shown to succeed against a weaker
configuration, which is why `test_redteam.py` asserts the OFF results too.

**M-20 · The echo guard was bypassed by quote marks (found by the red team).**
The tokenizer `[a-z0-9']+` made `'Reviewed` and `team'` different words, so
quoting the injected phrase dropped the copied run from 7 words to 5, below
the threshold. Fixed: apostrophes now count only inside words.

**M-21 · Two PII detector bugs, caught by smoke test before use.**
- "My name is …" wasn't matched, because the phrase was case-sensitive.
- `+1-555-0142` lost only its last 7 digits, leaving `+1-` behind.

**M-22 · A phone number at the end of a sentence wasn't redacted.**
The lookahead `(?![\w.])` treated the sentence's full stop as part of the
number, so "call me on +1-555-0142." passed straight through. The red-team
note happened to say "+1-555-0142 or …", so the attack test passed while the
detector had a gap. It was found by the per-guardrail unit tests written
afterwards; the fix is `(?!\w|\.\d)`. *Lesson:* end-to-end attacks test one
phrasing; detectors need their own table-driven tests.

**D-44 · Ablation: only the output guard is load-bearing against this attack suite.**
Each guardrail was removed alone:
- removing the output guard lets `classification_flip` and
  `instruction_echo` succeed;
- removing any other guardrail lets nothing through, because another layer
  catches it.

This is documented rather than hidden. `redact_pii` is still justified by
keeping PII out of model context and the trace (not measured by "attack
succeeded"). Argument limits and the denial abort make attacks fail faster
and more clearly. Spotlighting's value can only be measured on a real model.
The single point of failure for steering attacks is a known residual risk.

**M-23 · Test-suite runtime: I chased a slowdown and didn't fully pin it down.**
- The full suite went from 92 s (end of Phase 4) to about 9 minutes. The new
  Phase 5 tests explain only about 1–2 minutes of that.
- What I measured:
  - SQLite commits at the store's settings: 0.019 ms, so the disk is fine;
  - the same 2,154-event ingest standalone and inside pytest: both about 1 s;
  - an unchanged Phase 1 test: anywhere from 4.8 s to 13 s for identical work,
    with heavy CPU use from other applications on the machine at the time.
- Conclusion: it's most likely environmental load. **Not proven.**
- Action: a `slow` marker on the heavy suites (43 tests: pipeline faults, eval
  runner, red team, HTTP). The everyday loop `pytest -m "not slow"` runs 194
  tests in about 1¾ minutes; CI runs everything.
- Follow-up: re-time the full suite on a quiet machine before quoting it
  anywhere. **Phase 6 update:** the full suite, now 270 tests with the slow
  ones included, ran in **1 min 52 s**. That supports the environmental
  explanation.

**M-24 · On real Claude, the guardrails produced 3 false escalations out of 5 runs.**
- *Run:* 5 injection attacks against Claude Opus 5.5, guardrails off and on
  ($1.85). Claude resisted all of them on its own, even seeing the planted
  PII and not leaking it.
- *Problem:* with guardrails on, 3 of 5 **correct** reports were escalated.
- *Root causes:*
  1. **Phone pattern.** It allowed digit groups with no separators, so the
     13-digit epoch-millisecond timestamps Claude naturally quotes ("at
     1768216407600") matched 3+3+3+4. Fix: separators are required.
     Trade-off: an unformatted 10-digit phone is no longer caught (documented
     in the code).
  2. **Echo guard.** Claude *disclosed* the attack: "…a request to include
     the driver's details in the report. It was flagged…, and its request
     was not followed." That quoted 7 words of the note. Fix: a copied run is
     exempt only when the *rest of its sentence, outside the copied words*,
     discloses it ("request", "not followed", "flagged", and so on). Spotlight
     tags are stripped first, so a pasted note's own `<untrusted_driver_note>`
     tag can't supply the word "untrusted". A test covers an attacker hiding
     "requests" inside the text to be copied.
  3. **Validator** (same class as M-17/M-18): "the crash criterion, which
     requires the vehicle to stay at 0 m/s" had the threshold word 40+
     characters away. The policy-constant exemption now looks at the whole
     sentence instead of a ±30-character window.
- *Verification:*
  - the stored reports, re-checked offline with the fixed guard: 0 of 5 false
    escalations;
  - the gullible red team: still 0 of 9;
  - regression tests use Claude's verbatim sentences.
- *Lesson:* guardrails tuned against a scripted adversary must also be
  measured for false positives on the real model's prose. Precision matters
  as much as recall, because every false escalation costs a human review.
- *Tooling slip:* the backslash-unescaping trap hit again (a `"\n"` in a
  heredoc patch became a real line break and broke a string literal). Caught
  immediately because the module failed to parse. All patches now go through
  the Edit tool, and an AST and control-character scan runs after each batch.

## Phase 6: Observability, SLOs, API and UI

**D-45 · Telemetry is dependency-free, but uses standard wire formats.**
`driveguard/obs/` has JSON log lines, a counter/histogram registry rendered as
Prometheus text, and spans with W3C `traceparent` ids. OpenTelemetry would be
the production choice; here it would add an SDK, an exporter and a collector
for a single-process demo. Because the formats are standard, moving to OTel
changes the exporter, not the call sites.

Telemetry never carries payloads (event data, note text, prompts). Those live
in the audit trace, which has access control. Logs and metrics have none.

**D-46 · An investigation's trace id is its run id.**
`trace_id = UUID(run_id).hex`. A slow span in the logs then leads straight to
the hash-chained audit trace for the same run, with no join table.

Propagation:
- in process, contextvars follow the MCP SDK's per-send context snapshot
  (the same mechanism as M-8);
- over HTTP, the gateway sends `traceparent` and `TraceContextMiddleware`
  continues the trace on the server.

Tests prove both: every server-side `mcp_tool` span is a child of a client
`tool_call` span in the same trace.

**D-47 · SLOs are good/valid ratios, computed from durable data where possible.**
"95% of investigations finish within 90 s", never "mean latency 40 s".

- *From the stores:* tool latency and server faults (audit trace), run outcomes,
  latency and cost (`investigation_runs`), and ingest availability (broker
  tables). So the same report runs on any DriveGuard database, including the
  stored real-Claude eval runs.
- *From in-process metrics:* ingest latency and API latency only exist while a
  process runs, so `driveguard slo --bench` produces them.

**Not counted against availability:**
- producer errors (schema-invalid or conflicting events);
- FORBIDDEN or INVALID_ARGUMENT tool results;
- escalations, which are a correct, safe outcome.

A stuck `running` run (older than 10 min) counts as bad.

**D-48 · The reviewer API is read-only; `trace:read` is a new human-only scope.**
- *Framework:* Starlette, not FastAPI as designed, because it's already a
  dependency via the MCP SDK.
- *Tokens:* same JWT verifier, new audience `driveguard:api`.
- *`trace:read`:* human-only, enforced at both issue and verify time, because
  a trace holds every tool result a run saw.
- *Case-bound tokens:* see one case. Other or missing ids get an identical 403
  (no existence oracle).
- *Display redaction:* the trace is also PII-redacted for display, because
  runs made with guardrails off hold raw note PII.

This closes the Phase 5 residual risk "the trace has no access control".

**Approve/reject was left out of the UI (a change from the design).** A browser
write path needs CSRF protection and a real reviewer identity. A pasted bearer
token in session storage is not one. Reviews stay in `driveguard review`.
That's a known gap, listed in the README.

**D-49 · UI security: CSP without `unsafe-inline`; untrusted text is rendered only as text.**
- *Why it matters:* the trace contains attacker-written driver notes, so the
  UI is an XSS target.
- *Controls:*
  - CSP `script-src 'self'; style-src 'self'` (no inline styles anywhere);
  - no `dangerouslySetInnerHTML`;
  - hash routing, so the server has no catch-all route;
  - the token lives in `sessionStorage`, sent only to the same origin;
  - no CORS.
- *Test:* a component test renders `<img onerror>` and `<script>` note text
  and asserts no element is created.

### Mistakes / corrections

**M-25 · The budget's "a call can never overshoot the cap" guarantee was false on real traffic.**
- *Found by:* the new UI. Every real-Claude run showed a "token estimate below
  actual" guard span after each model call.
- *Measured:* 127 of 127 Opus 5.5 calls had actual input 1.36–1.84× the
  `chars/3` estimate (median 1.55×).
- *Root cause:* our prompts are JSON full of UUIDs and numbers, which tokenize
  at about 1.6–2.2 chars per token, not 3–4. M-11 named the assumption and
  added the guard span to expose it, but nobody looked until there was a UI. The scripted model counted
  `chars/4`, which hid the problem in every free run.
- *Fix:*
  - `CHARS_PER_TOKEN = 1.4`, so the worst observed call now uses 88% of the
    estimate;
  - the scripted model counts `chars/2` (realistic);
  - a regression test on UUID-heavy JSON.
- *Checked offline* against the stored runs: no run would have tripped the
  $0.50 cap by mistake (minimum headroom $0.061).
- *Lesson:* a safety margin stated in a docstring is a hypothesis until it's
  measured against the real system.

**M-26 · `python -m driveguard.cli` from the workspace root imported the wrong package.**
- *Symptom:* the preview launcher starts servers from `C:\Personal\Projects`.
  There, `-m` puts the cwd on `sys.path`, so `driveguard` resolved to the
  *project folder* as a namespace package, and `driveguard.evals` to the
  `evals/` data directory.
- *Fix:* launch the installed `driveguard` console script, whose `sys.path[0]`
  is its own directory.

**M-27 · I assumed pre-1.0 Starlette and pre-3.12 f-strings, twice.**
- *Starlette 1.7:* it removed `@app.middleware`. Security headers are now a
  pure ASGI middleware.
- *Python 3.10 f-strings:* the Prometheus renderer nested an f-string with
  escaped quotes, which 3.10 rejects. Caught at import, before any test ran.

The same lesson as M-5: check the installed version's API, not memory.

**M-28 · UI readability bugs found in the browser, not in tests.**
- "4 in · 83 out" understated model input, because almost all of it came
  from the prompt cache. It now shows total input with the cached part.
- The evidence panel covered the very timeline step it had just highlighted.
  It moved to the report column.

Both passed type checks and unit tests. Only looking at the running app
caught them.

A third one came from self-review: when a token expired, every request failed
but the UI stayed "signed in" with the stale token in storage. A 401 now
clears the token and returns to sign-in, while a 403 keeps the session. Both
cases are tested.

**D-50 · Speeds are stored in m/s and reported in mph, and both units are verified.**
- *Why:* the reviewers are US-based and read speed in mph. Sensors and the
  policy are in SI units, so storage stays in m/s and conversion happens only
  at the edges.
- *Reports:* the writer states "28.6 mph (12.8 m/s)", so the reader gets mph
  and the m/s value still matches the raw event exactly.
- *Validator:* `mph` is now a verified unit, with a tolerance of 0.7 mph
  (about 0.3 m/s, the same as for m/s). This is the important part: just
  telling the model to write mph would have let every mph number through
  unchecked, because the validator only verified units it recognised.
- *Policy thresholds:* thresholds quoted in another unit ("the 11.2 mph
  phone-distraction threshold", which is 5 m/s) are recognised after
  conversion.
- *UI:* GPS evidence shows speed and limit in mph with the stored m/s value.
- *Verified on real Claude* (3 mini cases, $0.60):
  - every speed came out in the requested form;
  - all conversions were correct;
  - all 3 reports passed the citation check on the first draft (22–33 numbers
    checked each);
  - **0** "token estimate below actual" guard spans, where every call had one
    before M-25. That fix is now confirmed on real traffic.

**M-29 · A patch that silently did nothing, caught by its own tests.**
The first mph patch changed the docs and the matching code, but not the unit
regex. The shell's backslash unescaping changed the search string, so
`str.replace` found no match and did nothing. mph claims then went through
completely unchecked ("Speed was 35 mph" passed), and only the new negative
tests caught it.
- *Fix:* the regex edit was redone with the edit tool.
- *Rule:* scripted patches now `assert old in text` before replacing.
- *Lesson:* a feature is only as good as its negative tests; the "wrong
  value is caught" cases exposed the no-op.

**D-51 · Approve/reject moved into the UI.**
`POST /api/cases/{id}/review` enforces the same rules as `driveguard review`:
- scope `cases:review`, which is human-only at both issue and verify time;
- a case-bound token can only review its own case;
- the reviewer must send the case version they saw (optimistic locking).
  A stale version or an illegal transition returns 409, and the UI says "this
  case changed, reload";
- a required comment and a JSON body (form posts get 415).

The decision is appended to the run's hash-chained trace as a
`human_decision` span, so the chain still verifies with the decision in it.

Reads keep their `mode=ro` connection; the review uses a separate write
connection.

**D-52 · `investigate --all` re-runs rejected cases first.**
`pending_incidents()` selects new incidents plus cases in INVESTIGATING with
no live run. That covers a human rejection, or a run that died. Rejected cases
go first, so a reviewer's correction doesn't queue behind new work. The
reviewer's comment reaches the agents as `reviewer_feedback` (test:
`test_investigate_all_picks_up_rejected_cases_with_feedback`).

**M-30 · D-48 kept approve/reject out of the UI for the wrong reason.**
- *What D-48 said:* a browser write path "needs CSRF protection".
- *Why that was wrong:* CSRF exploits credentials the browser attaches
  automatically, such as cookies. This API authenticates with a bearer header
  that only our own script sets, so a cross-site page can't make an
  authenticated request.
- *What the real concern is:* the reviewer's identity is a minted token, not
  SSO. That's a production gap, not a reason to keep reviews in the CLI, where
  the identity is the same token.
- *Defence in depth anyway:* the endpoint requires `application/json`.

**M-31 · A rejected case was stranded, found by walking the reviewer's loop end to end.**
- *Bug:* reject sets the case to INVESTIGATING, but `investigate --all`
  selected only incidents *without* a case, so nothing ever re-ran it.
- *Why no test caught it:* every component test passed. The rerun test called
  `investigate(incident_id)` directly, so the selection logic was never part
  of it.
- *Fix:* D-52.
- *Lesson:* test the user's workflow, not just the units.


**M-32 · On the full dev split, my own guardrail stopped 3 of 8 phone-distraction cases.**
- *Run:* Claude Opus 5.5 on all 40 dev cases, 1 sample, $8.89.
- *Result:*
  - 37 of 40 correct, 0 wrong answers;
  - all 3 misses were escalations;
  - selective accuracy 1.000;
  - 0 fabricated ids.
- *Root cause:* to judge phone distraction, the evidence agent asked for the
  trip's `phone` and `driver_note` events across the whole trip. The
  argument-limit guardrail (D-43) exempted only `["driver_note"]`, so it
  refused. The agent couldn't know the rule existed. After a second refusal,
  the denial-abort guardrail stopped it as "possible manipulation". The
  scripted agent never makes this call, so 240 scripted case-samples showed 0
  false escalations.
- *Fix:*
  - The limit now targets what it was meant for: the high-rate streams (gps
    and accel, hundreds of events per trip). Requests for only sparse types
    (phone, brake, driver_note, trip_start/end, at most about 6 per trip)
    need no time window.
  - The rule is stated in the triage and evidence prompts, so it isn't
    discovered by being refused.
  - Tests use Claude's verbatim requests. The red team is still 0 of 9 with
    guardrails on: the flood attack is still refused.
- *Also from this run:*
  - One first draft was flagged for "an impact near 4 g". That's reasoning
    about the crash threshold, and the validator lacked "neither"/"near" as
    threshold wording. Fixed, and re-checked offline ($0).
  - The other repair was a genuine citation slip: a "0.599 g" claim citing
    only GPS events. The validator correctly still flags it.
- *Lesson (the third time, after M-24 and M-25):* every guardrail needs a
  false-positive measurement on the real model. The scripted agent only
  exercises the paths I thought of.
- *Confirmation run (dev, $8.39):*
  - Phone recall went from 0.625 to 0.750, but 5 of 40 cases were still
    escalated as "possible manipulation". So the M-32 fix was incomplete.
  - The held-out test run (2 samples, $16.86) was queued behind it with the
    same code, and shows the same pattern: accuracy 0.925, selective accuracy 1.000, and 6 of 80
    case-samples escalated this way. The cause is M-33.

**M-33 · Argument refusals were treated as an attack signal, and then my first fix didn't work.**

- *Found by:* the M-32 confirmation run on dev.
- *What happened:* in every remaining escalation, Claude asked for a small
  gps or accel sample with no time window (`{"types": ["gps"], "limit": 10}`).
  It was refused, sent the identical request once more, and the 2-denial
  abort stopped the agent.
- *Root cause:* two guardrails were coupled that shouldn't be.
  - The denial abort (D-44) exists because an agent repeatedly asking for
    things it may not have (another trip, a tool off its allowlist) is a sign
    of injection.
  - An argument-limit refusal is a usage error. The request was for the
    agent's own trip.
  - Counting both as "denials" meant one ignored refusal ended the
    investigation.
- *Fix, attempt 1 (didn't work):*
  - Argument refusals stopped counting toward the manipulation abort, and got
    their own cap of 3.
  - The refusal gave the exact window to retry with, and called out a
    repeated request.
  - Dev and test re-runs ($9.58 and $9.73) still escalated 6 of 45 and 4 of
    46 case-samples.
  - The traces show Claude received the exact retry window and still sent
    the identical request three times, at low effort with no visible
    reasoning. My assumption that a clearer refusal would be followed was
    wrong for this model setting.
- *Fix, attempt 2:*
  - A gps/accel listing with no window is no longer refused. The gateway
    narrows it to the incident ±1 minute (never more than 10 minutes),
    records a `narrowed` guard span, and adds a `gateway_note` to the result.
  - The bound on what the agent can read is unchanged, and the server only
    ever sees the bounded window. The flood attack's "unbounded whole-trip
    reads" stay at 0, because the trace records the narrowed arguments.
  - An explicit window that is too wide is still refused (a deliberate
    request for more), with the 3-refusal cap.
  - Access denials still abort after 2.
  - Tests replay Claude's verbatim request, re-sent 3 times: all 3 are
    narrowed, with 0 refusals.
- *Result (held-out test, 40 cases × 1 sample, $8.55):*
  - 40 of 40 correct, with 0 escalations and 0 fabricated ids;
  - the narrowing fired 6 times, every one in a case that went on to be
    correct;
  - phone_distraction recall went from 0.750 to 1.000.
  - Caveat: this was the third look at the test split, after two runs that
    were each stopped by this bug. All three are reported in EVALS.md.
- *Lesson:* for an LLM caller, prefer a guardrail that **constrains** the
  request to the safe form over one that **refuses** it and relies on the
  model to correct itself. The refusal is safe, but it costs the
  investigation.
- *Eval hygiene:* the fix was derived from dev traces only. The test failures
  were read only to confirm they had the same cause. A second test pass after
  this fix is reported alongside the first one, not instead of it.
