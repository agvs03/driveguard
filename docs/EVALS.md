# DriveGuard evaluation

How quality is measured, what the numbers are, and what they do *not* show.
Metric definitions live in [`driveguard/evals/metrics.py`](../driveguard/evals/metrics.py).

## Datasets

| split | seed | cases | use |
|---|---|---|---|
| `dev` | 101 | 40 (8 per class) | error analysis and iteration |
| `test` | 202 | 40 (8 per class) | held out: the numbers we quote |
| `mini` | 303 | 10 (2 per class) | CI regression gate; first real-model run |

**What a case is.** Each case is one *detected* incident, which is what the
agents actually see.

**How ground truth is set:**
- A detection that overlaps an injected incident gets that incident's kind.
- A detection that overlaps a hard negative (a pothole or a firm brake that
  fooled the detector), or overlaps nothing, gets `no_incident`.

Each split has 8 hard-negative cases. Dataset JSON lives in
[`evals/datasets/`](../evals/datasets). Databases are rebuilt from the seed
and checked against a fingerprint.

## Metrics

All metrics are computed over k samples per case, with 95% bootstrap CIs that
resample cases. Before/after comparisons use a paired bootstrap on the
delta.

| metric | what it answers |
|---|---|
| accuracy, macro-F1, confusion matrix | Is the classification right? Escalations count as wrong. |
| coverage, selective accuracy | How often does it decide, and how good are the decisions it makes? |
| citation precision | Do the cited events fall inside the true incident window? |
| evidence hit rate | Does the report cite at least one of the injected incident's own events? |
| **hallucination rate (first draft)** | How often does the *model* cite a fabricated or ungrounded event, or misstate a number? |
| **shipped hallucination rate** | How often does such an error *reach a human*? This should be 0, because of the citation gate. |
| fabricated-ID rate, repair rate, escalation rate | Where errors come from, and what the safety net costs. |
| stability | Do repeated samples agree? |
| cost and latency per case, model and tool calls | What it takes to run. |

## The regression gate ("run the evals on every change")

`tests/test_eval_runner.py::test_no_regression_against_committed_baseline`
runs the full pipeline on `mini` with the free scripted LLM, compares it
against [`evals/baselines/scripted-mini.json`](../evals/baselines/scripted-mini.json),
and fails pytest on a regression. The rules:
- **Quality metrics** must have the whole 95% CI of the delta beyond the
  tolerance before they count as a regression.
- **Safety metrics** fail on the point estimate alone.

Accept an intended change with:

```bash
driveguard eval gate --split mini --update
```

## Results

### Scripted baseline: a consistency check, not a skill measurement

The scripted analyst applies the classification policy literally, and the v2
labels follow the same policy. So its accuracy is a **ceiling**: it shows
the pipeline, tools, trace and validator work end to end. It says nothing
about judgement.

| run (k = 3) | accuracy | macro-F1 | citation precision | evidence hit | hallucination (first draft / shipped) |
|---|---|---|---|---|---|
| dev, before change | 1.000 | 1.000 | 1.000 | 0.750 | 0.000 / 0.000 |
| **test, before change** | 1.000 | 1.000 | 1.000 | 0.750 | 0.000 / 0.000 |
| dev, after change | 1.000 | 1.000 | 1.000 | **1.000** | 0.000 / 0.000 |
| **test, after change** | 1.000 | 1.000 | 1.000 | **1.000** | 0.000 / 0.000 |

**The change:** the report must cite the event that directly shows the
behaviour, for example the phone `screen_on` event. Evidence hit rate on
test went from 0.75 to 1.00, with a 95% CI on the delta of +0.10 to +0.39 and
no regressions.

**Fault demo** (dev, the writer hallucinates on its first draft):

| | rate |
|---|---|
| first-draft hallucination | 0.77 |
| fabricated IDs | 0.36 |
| shipped hallucination | **0.00** |
| repair | 1.00 |

The citation gate caught everything, at the cost of one extra model call per
case. The regression gate flagged both safety metrics.

**Guardrails on vs off** (Phase 5, scripted, k = 3, dev and test): accuracy,
F1, citation precision and evidence hit rate are all unchanged at 1.000.
Escalation rate is unchanged at **0.000**, so there were no false positives
on 240 clean case-samples. Model calls per case went from 7 to 8, because the
evidence agent now reads driver notes. Gate: no regressions. The red-team
results are in [SECURITY.md](SECURITY.md).

### Claude Opus 5.5

Held-out `mini` split: 10 cases × 2 samples. One case-sample was skipped by the
$5 run cap. Total spend: **$4.64**.

| metric | value (95% CI) |
|---|---|
| accuracy / macro-F1 | **1.000** (19/19; CI 1.00 .. 1.00) |
| coverage, escalation rate | 1.000, 0.000 |
| citation precision | 0.959 (0.931 .. 0.985): Claude also cites context outside the window, such as pre-incident speed |
| evidence hit rate | 1.000 |
| fabricated-ID rate | **0.000** |
| hallucination rate, as measured live | 0.026 (0.007 .. 0.045) |
| hallucination rate, re-scored with the fixed validator | **0.000**: every flag was a validator false positive (M-17, M-18) |
| shipped hallucination rate | 0.000 |
| repair rate | 0.211: all 4 repairs were triggered by validator false positives |
| stability across samples | 1.000 |
| cost per case | **$0.244** ($0.22 .. $0.26) |
| latency p50 / p95 | **58 s / 73 s** (SLO target from design: p95 < 90 s) |
| model / tool calls per case | 6.7 / 4.8 |

**Reading this honestly:**
- **Small sample.** With 10 cases, an accuracy of 1.00 has a real-world CI far
  wider than the bootstrap shows; the bootstrap can't see beyond these 10
  cases.
- **Easy cases.** The cases are synthetic and clear-cut by construction.
- **The result to trust most is the zero fabricated IDs.** On real model
  output, the system produced no fabricated citations, and the cost and
  latency match the Phase 0 budget.
- **The validator is the noisy component.** Every "hallucination" flag the
  live run produced was the validator misreading correct reasoning about a
  threshold that wasn't met.

## Error analysis log

1. **v1 dev: 15/24 `no_incident` predicted as `hard_braking`.** The label was
   wrong, not the system: firm-brake negatives had peaks above the 0.45 g
   policy threshold. Fixed in the simulator, and datasets bumped to v2
   (DECISIONS M-14).
2. **v2 dev: 24/24 phone cases correct but missing the decisive evidence.**
   Reports cited a speed sample, not the screen-on event. Fixed in the writer
   prompt and the scripted writer (D-36).

3. **Claude on `mini`: 4 citation repairs, all validator false positives.**
   Each flagged sentence was correct reasoning about a threshold that was
   *not* met ("rules out a crash pattern of staying at 0 m/s", "do not
   approach a 4 g resultant", "instead of remaining at 0 m/s"). Fixed by
   deriving policy constants from the policy text and widening the
   counterfactual wording. All 153 first drafts were re-scored offline from
   the trace at $0, giving 0 remaining flags.
4. **A side effect of a strict validator.** When repairing, Claude removed the
   threshold numbers from its prose ("rather than remaining stopped"). The
   reports stay correct but become less specific.

## Limitations

- **Synthetic, self-labeled data.** The simulator, the detector and the
  policy were all written by the same author, so there are no real-world
  failure modes.
- **Hallucination checks are mechanical.** They verify event IDs, grounding
  and numbers with units. Qualitative claims ("the driver appeared
  distracted") are not checked; that would need an LLM judge validated
  against human labels.
- **The test split is not genuinely unseen.** Its cases were never inspected,
  but the simulator fix (M-14) was motivated by dev and applied to all
  splits.
- **Real-model samples are few** because of cost, so their CIs are wide.
