"""Step 11 tests: the evaluation harness measures honestly against gold labels."""

import pytest

from portfoliopulse import evaluation_fixtures as fx
from portfoliopulse.evaluation import run_evaluation, write_report


@pytest.fixture(scope="module")
def full_run():
    """One harness execution over the whole 10-company dataset (module-scoped)."""
    metrics, details = run_evaluation()
    return metrics, details


def test_full_dataset_metrics_meet_spec_targets(full_run):
    metrics, details = full_run
    # spec targets (docs/evaluation.md): precision >= 0.9, no unsupported material
    # in approved output, conflicts disclosed, duplicates near zero.
    assert metrics.n_companies == 10
    assert not metrics.runs_failed
    assert metrics.claim_support_precision >= 0.9
    assert metrics.claim_support_precision < 1.0  # the two "partly" traps must bite
    assert metrics.citation_relevance >= 0.95
    assert metrics.unsupported_rate_in_approved == 0.0
    assert metrics.conflicts_disclosed == 2 and metrics.n_gold_conflicting == 2
    assert metrics.duplicate_source_rate == 0.0
    assert metrics.duplicate_event_rate == 0.0
    # exactly one gold-dated event (prose-date trap) has no system event_date
    assert metrics.gold_dated_events == 10 and metrics.date_errors == 1
    assert metrics.missing_or_incorrect_date_rate == 0.1
    assert metrics.hypotheses_drafted == 9  # Kaunas has no 2nd supported acquisition claim
    assert len(details) == 10 and all(d["status"] == "approved" for d in details)


def test_human_only_metrics_are_reported_not_measured(full_run, tmp_path):
    metrics, _details = full_run
    assert metrics.human_usage_ratings is None
    assert metrics.cost_per_run is None
    report = write_report(metrics, tmp_path / "r.md")
    assert "not measured" in report
    assert "Measured results" in report


def test_report_contains_per_company_table(full_run, tmp_path):
    metrics, details = full_run
    report = write_report(metrics, tmp_path / "r.md", details)
    assert "| Kaunas Electronics | approved | 3 | 5 | 0 |" in report
    assert "| Ferrol Castings | approved | 2 | 4 | 1 |" in report


def test_subset_run_is_isolated_and_consistent(tmp_path):
    """Any subset must produce internally consistent metrics (no dataset coupling)."""
    cases = [fx.build_company_cases()[i] for i in (0, 2, 9)]  # aalborg, carmona, kaunas
    metrics, details = run_evaluation(cases=cases)
    assert metrics.n_companies == 3 and len(details) == 3
    # aalborg 3 supported + carmona 3 (1 partly) + kaunas 3 supported → 8/9
    assert metrics.system_supported == 9
    assert metrics.gold_supported_in_system_supported == 8
    assert metrics.claim_support_precision == pytest.approx(8 / 9, abs=1e-9)
    assert metrics.conflicts_disclosed == 2
    assert metrics.unsupported_rate_in_approved == 0.0
    assert metrics.missing_or_incorrect_date_rate == 0.0  # subset has no prose-date trap
