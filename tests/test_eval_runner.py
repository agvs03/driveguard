"""End-to-end harness tests on a small dataset built in a temp directory."""

import dataclasses
import json
import sqlite3

import pytest

from driveguard.agents.budget import Limits, Price
from driveguard.agents.scripted import ScriptedClient
from driveguard.evals.dataset import REPO_DATASETS, Dataset, build_dataset, ensure_db
from driveguard.evals.metrics import CaseResult, aggregate, compare, regressions
from driveguard.evals.runner import load_results, open_eval_db, run_eval

pytestmark = pytest.mark.slow


@pytest.fixture(scope="module")
def mini(tmp_path_factory):
    d = tmp_path_factory.mktemp("evalmini")
    ds = build_dataset("mini", d / "mini.db", per_class=1)
    return ds, d / "mini.db"


async def _run(mini, tmp_path, label, llm=ScriptedClient, samples=1, **kw):
    ds, db = mini
    run_id = await run_eval(ds, base_db=db, llm_factory=llm, samples=samples, label=label,
                            eval_db=tmp_path / "evals.db", runs_dir=tmp_path / "runs", **kw)
    return run_id, load_results(open_eval_db(tmp_path / "evals.db"), run_id)


def test_dataset_has_one_case_per_class_and_is_reproducible(mini, tmp_path):
    ds, _ = mini
    assert sorted(c.expected for c in ds.cases) == sorted(["hard_braking", "crash", "near_miss",
                                                           "phone_distraction", "no_incident"])
    again = build_dataset("mini", tmp_path / "again.db", per_class=1, reuse_db=False)
    assert again.dataset_hash == ds.dataset_hash and again.db_fingerprint == ds.db_fingerprint


def test_ensure_db_refuses_a_mismatched_database(mini, tmp_path):
    ds, _ = mini
    wrong = dataclasses.replace(ds, db_fingerprint="0000000000000000")
    with pytest.raises(RuntimeError, match="does not match"):
        ensure_db(wrong, mini[1])


async def test_run_scores_every_case_sample_and_is_deterministic(mini, tmp_path):
    run_a, res_a = await _run(mini, tmp_path, "a", samples=2)
    run_b, res_b = await _run(mini, tmp_path, "b", samples=1)
    assert len(res_a) == 2 * len(mini[0].cases)
    assert [r.predicted for r in res_a if r.sample == 0] == [r.predicted for r in res_b]
    meta = open_eval_db(tmp_path / "evals.db").execute("SELECT * FROM eval_runs WHERE run_id = ?", (run_a,)).fetchone()
    assert meta["dataset_hash"] == mini[0].dataset_hash and len(meta["code_fingerprint"]) == 16
    assert json.loads(meta["summary_json"])["n_case_samples"] == len(res_a)
    for r in res_a:  # every failing row points at an inspectable trace
        assert (tmp_path / "runs" / run_a / f"sample-{r.sample}.db").exists()


async def test_gate_catches_a_regressed_candidate(mini, tmp_path):
    _, base = await _run(mini, tmp_path, "base")
    _, cand = await _run(mini, tmp_path, "cand", llm=lambda: ScriptedClient({"hallucinate_always"}))
    agg = aggregate(cand, reps=50)
    assert agg["metrics"]["escalation_rate"]["value"] == 1.0, "every report fails validation twice -> escalated"
    assert agg["metrics"]["hallucination_rate"]["value"] > 0
    assert agg["metrics"]["shipped_hallucination_rate"]["value"] is None, "nothing invalid reached a human"
    bad = regressions(compare(base, cand))
    assert any(b.startswith("accuracy") for b in bad) and any(b.startswith("hallucination_rate") for b in bad)


async def test_run_cost_cap_skips_and_reports_unscheduled_cases(mini, tmp_path):
    pricey = {"scripted": Price(4.0, 20.0, 0, 0)}
    run_id, res = await _run(mini, tmp_path, "capped", run_cost_cap=0.6, prices=pricey,
                             limits=dataclasses.replace(Limits(), max_cost_usd=0.5))
    skipped = [r for r in res if r.status == "skipped_budget"]
    assert skipped and len(skipped) < len(res)
    meta = open_eval_db(tmp_path / "evals.db").execute("SELECT total_cost_usd FROM eval_runs WHERE run_id = ?",
                                                       (run_id,)).fetchone()
    assert meta[0] <= 0.6
    assert aggregate(res, reps=10)["skipped_budget"] == len(skipped)


async def test_no_regression_against_committed_baseline(tmp_path):
    """'Run the evals on every change': the scripted pipeline on the committed mini split
    must not regress against the committed baseline (update with `driveguard eval gate --update`)."""
    path = REPO_DATASETS.parent / "baselines" / "scripted-mini.json"
    if not path.exists():
        pytest.skip("no committed baseline yet")
    ds = Dataset.load(REPO_DATASETS / "evalset-v2-mini.json")
    db = ensure_db(ds, tmp_path / "mini.db")
    run_id = await run_eval(ds, base_db=db, llm_factory=ScriptedClient, samples=1, label="ci-gate",
                            eval_db=tmp_path / "evals.db", runs_dir=tmp_path / "runs")
    cand = load_results(open_eval_db(tmp_path / "evals.db"), run_id)
    base = [CaseResult(**r) for r in json.loads(path.read_text(encoding="utf-8"))]
    cmp = compare(base, cand)
    assert not regressions(cmp), regressions(cmp)
