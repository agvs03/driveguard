# Getting started with DriveGuard

A guided first run for someone new to the project. Allow about 10 minutes. No
API key and no cost: everything here uses the built-in scripted model.

## What DriveGuard is for

Fleets, ride-share operators and insurers collect telematics (speed,
acceleration, GPS, phone use) from every trip. That produces thousands of
alerts a day, like "hard braking at 14:02" or "possible crash", and a safety
analyst has to work out what really happened.

DriveGuard drafts that investigation automatically. AI agents gather the
evidence and write a report in which every claim cites the sensor event
behind it. The citations are checked by code, and **a human always makes the
final decision.**

It's a working prototype on **synthetic data only**, and a reference for
building AI agents safely: least privilege, guardrails, audit trails, evals
and SLOs. The same "AI drafts, human decides" pattern fits insurance claims,
fraud alerts, compliance reviews and support triage.

## 1. Install

You need Python 3.10+ and Node 20+.

```bash
git clone git@github.com:agvs03/driveguard.git
cd driveguard
python -m venv .venv
```

Activate the environment:
- Windows: `.venv\Scripts\activate`
- macOS/Linux: `source .venv/bin/activate`

```bash
pip install -e ".[dev]"
pytest -m "not slow"          # should end with "239 passed"
```

## 2. Generate data and ingest it

```bash
driveguard simulate --out data/run1 --seed 7 --trips 20
driveguard ingest --data data/run1 --db data/driveguard.db --faults
```

`--faults` deliberately breaks the stream with duplicates, reordering,
corrupt messages and failing writes. The summary shows retries and
dead-lettered messages, and no lost events.

## 3. Let the agents investigate

```bash
driveguard investigate --db data/driveguard.db --all --limit 5
```

Each line is one case. `"status": "awaiting_approval"` means the report
passed every check and now waits for a human. Copy one `case_id`.

```bash
driveguard report --db data/driveguard.db --case <case_id>              # the report, as markdown
driveguard trace  --db data/driveguard.db --case <case_id>              # every step, plus the tamper check
driveguard review --db data/driveguard.db --case <case_id> --decision approve --comment "looks right" --reviewer you
```

## 4. Open the reviewer UI

Build the UI once:

```bash
cd web && npm install && npm run build && cd ..
```

Set a signing secret for this terminal session. Any random string of 32+
characters works.
- macOS/Linux: `export DRIVEGUARD_TOKEN_SECRET=$(python -c "import secrets; print(secrets.token_urlsafe(48))")`
- Windows (cmd): `for /f %s in ('python -c "import secrets; print(secrets.token_urlsafe(48))"') do set DRIVEGUARD_TOKEN_SECRET=%s`

Mint a reviewer token, then start the API:

```bash
driveguard token --sub human:you --role human --scopes cases:read,trace:read --aud api --ttl 28800
driveguard api --db data/driveguard.db
```

Open http://127.0.0.1:8100 and paste the token. Pick a case, then:
- **click a citation** under a finding to see the exact sensor event and the
  tool call that returned it;
- **expand the timeline steps** to see every model call, tool call and check
  the agents made.

## 5. Try the safety and quality tooling

```bash
driveguard redteam                     # 9 attacks (prompt injection, PII theft, ...) with guardrails off vs on
driveguard eval gate                   # regression gate: accuracy, grounding, cost vs the stored baseline
driveguard slo --bench                 # 500 events/s load test plus the SLO report
driveguard slo --bench --faults        # the same under injected failures: watch the error budgets burn
```

## 6. Optional: real Claude

Create a file called `.env` (it's git-ignored) containing your own key:

```
ANTHROPIC_API_KEY=sk-ant-...
```

Then run:

```bash
driveguard eval run --split mini --label my-claude-run --llm claude --env-file .env --samples 1
```

- **Cost:** about $0.25 per case on Claude Opus 5.5. The hard caps are $0.50
  per case and $5 per run (`--max-run-cost`).
- **Key handling:** only `ANTHROPIC_API_KEY` and `ANTHROPIC_WORKSPACE_ID` are
  read from the file, and the key is never printed or logged.

## Troubleshooting

| Problem | Fix |
|---|---|
| pytest or Hypothesis crashes on Python 3.10.0 | Keep the pinned versions from `pyproject.toml` (a 3.10.0 bug), or use Python 3.10.1+ |
| `DRIVEGUARD_TOKEN_SECRET must be set` | Set it in the same terminal that runs `driveguard api` / `driveguard token` |
| UI says "missing, invalid or expired bearer token" | The token was minted with a different secret or has expired: mint a new one |
| UI is blank at http://127.0.0.1:8100 | Run `npm run build` in `web/` first (the API serves `web/dist`) |
| `investigate` prints nothing | Every incident already has a case: simulate with another `--seed` into a new `--db` |

Next, read [the design](DESIGN.md), and the [decisions and mistakes log](DECISIONS.md)
for why things are built this way.
