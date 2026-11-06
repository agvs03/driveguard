import os

import pytest

from driveguard.evals.cli import load_env_file
from driveguard.evals.dataset import label_incident
from driveguard.evals.metrics import METRICS, CaseResult, aggregate, bootstrap_ci, bucket, compare, regressions, stability
from driveguard.sim.generator import Label


def R(key, expected, predicted, sample=0, status="awaiting_approval", **kw):
    r = CaseResult(case_key=key, sample=sample, expected=expected, truth_source="incident", detected_kind=expected,
                   predicted=predicted, status=status, reason=kw.pop("reason", ""), correct=predicted == expected,
                   decided=predicted is not None, **kw)
    r.bucket = bucket(r)
    return r


def test_accuracy_counts_abstentions_as_wrong_and_coverage_separates_them():
    rs = [R("a", "crash", "crash"), R("b", "crash", None, status="escalated", reason="budget: x"),
          R("c", "near_miss", "hard_braking"), R("d", "no_incident", "no_incident")]
    assert METRICS["accuracy"](rs) == 0.5
    assert METRICS["coverage"](rs) == 0.75
    assert METRICS["selective_accuracy"](rs) == pytest.approx(2 / 3)


def test_macro_f1_hand_computed():
    rs = [R("a", "crash", "crash"), R("b", "crash", "near_miss"), R("c", "near_miss", "near_miss")]
    # crash: p=1, r=.5 -> f1=.667 ; near_miss: p=.5, r=1 -> f1=.667
    assert METRICS["macro_f1"](rs) == pytest.approx(2 / 3)


def test_hallucination_rate_uses_first_drafts_and_shipped_rate_uses_valid_reports():
    a = R("a", "crash", "crash", first_draft_findings=4, first_draft_hallucinated_findings=1,
          shipped_valid=True, shipped_findings=3, shipped_hallucinated_findings=0)
    b = R("b", "crash", "crash", first_draft_findings=4, first_draft_hallucinated_findings=0,
          shipped_valid=True, shipped_findings=4, shipped_hallucinated_findings=0)
    assert METRICS["hallucination_rate"]([a, b]) == 1 / 8
    assert METRICS["shipped_hallucination_rate"]([a, b]) == 0


def test_bootstrap_resamples_cases_and_is_seeded():
    rs = [R(f"c{i}", "crash", "crash" if i % 2 else "near_miss") for i in range(20)]
    ci1, ci2 = bootstrap_ci(rs, "accuracy", seed=1), bootstrap_ci(rs, "accuracy", seed=1)
    assert ci1 == ci2 and ci1[0] < 0.5 < ci1[1]
    same = [R(f"c{i}", "crash", "crash") for i in range(5)]
    assert bootstrap_ci(same, "accuracy") == (1.0, 1.0)


def test_stability_across_samples():
    rs = [R("a", "crash", "crash", sample=0), R("a", "crash", "crash", sample=1), R("a", "crash", "near_miss", sample=2),
          R("b", "crash", "crash", sample=0), R("b", "crash", "crash", sample=1), R("b", "crash", "crash", sample=2)]
    assert stability(rs) == pytest.approx((2 / 3 + 1) / 2)


def test_compare_and_regression_gate():
    base = [R(f"c{i}", "crash", "crash", first_draft_findings=2) for i in range(10)]
    worse = [R(f"c{i}", "crash", "crash" if i < 6 else "near_miss", first_draft_findings=2,
               first_draft_hallucinated_findings=1 if i < 3 else 0) for i in range(10)]
    cmp = compare(base, worse)
    assert cmp["metrics"]["accuracy"]["delta"] == pytest.approx(-0.4)
    bad = regressions(cmp)
    assert any(b.startswith("accuracy") for b in bad) and any(b.startswith("hallucination_rate") for b in bad)
    assert len(cmp["changed_predictions"]) == 4
    tiny = [R(f"c{i}", "crash", "crash" if i else "near_miss", first_draft_findings=2) for i in range(100)]
    base100 = [R(f"c{i}", "crash", "crash", first_draft_findings=2) for i in range(100)]
    assert not regressions(compare(base100, tiny)), "a 1-point drop is within the 2-point tolerance"
    assert not regressions(compare(base, base))


def test_buckets():
    assert bucket(R("a", "crash", None, status="escalated", reason="budget: cost cap")) == "escalated:budget"
    assert bucket(R("a", "no_incident", "hard_braking")) == "wrong:no_incident->hard_braking"
    assert bucket(R("a", "crash", "crash", evidence_hit=False)) == "correct_but_missed_evidence"
    assert bucket(R("a", "crash", "crash", evidence_hit=True)) == ""


def test_aggregate_excludes_budget_skips():
    rs = [R("a", "crash", "crash"), R("b", "crash", None, status="skipped_budget")]
    agg = aggregate(rs, reps=50)
    assert agg["n_case_samples"] == 1 and agg["skipped_budget"] == 1 and agg["metrics"]["accuracy"]["value"] == 1.0


def _L(kind, t0, t1, trip="t", inc=True):
    return Label("l-" + kind, trip, kind, inc, t0, t1, ("e",))


def test_labeling_rules():
    inc = {"trip_id": "t", "t_start_ms": 10_000, "t_end_ms": 11_000}
    assert label_incident(inc, [_L("near_miss", 10_500, 11_500)])[:2] == ("near_miss", "incident")
    assert label_incident(inc, [_L("pothole", 10_000, 10_500, inc=False),
                                _L("hard_braking", 9_000, 12_000)])[0] == "hard_braking", "incident beats negative"
    assert label_incident(inc, [_L("pothole", 10_000, 10_500, inc=False)])[:2] == ("no_incident", "hard_negative")
    assert label_incident(inc, [_L("crash", 12_500, 13_000)])[:2] == ("crash", "incident"), "within 2 s slack"
    assert label_incident(inc, [_L("crash", 14_000, 15_000)])[:2] == ("no_incident", "unlabeled")
    assert label_incident(inc, [_L("crash", 10_000, 11_000, trip="other")])[1] == "unlabeled"


def test_env_file_loads_only_the_api_key(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OTHER_SECRET", raising=False)
    f = tmp_path / ".env"
    f.write_text('# comment\nexport ANTHROPIC_API_KEY="sk-ant-test-value-123"\nOTHER_SECRET=x\nWORKSPACE_ID=w\n')
    assert load_env_file(str(f)) == ["ANTHROPIC_API_KEY"]
    assert os.environ["ANTHROPIC_API_KEY"] == "sk-ant-test-value-123"
    assert "OTHER_SECRET" not in os.environ and "WORKSPACE_ID" not in os.environ
    assert "sk-ant-test" not in capsys.readouterr().out
