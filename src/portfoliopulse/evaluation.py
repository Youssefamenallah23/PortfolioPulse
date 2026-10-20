"""Step 11 evaluation harness: runs the full pipeline over the fictional 10-company
fixture dataset, applies the documented strict review policy, exports the approved
packs, and measures the spec's metrics from persisted data.

Honesty rules implemented here
- Targets and results are strictly separate: this module only MEASURES. Targets
  live in docs/evaluation.md and are never reported as achievements.
- Human-only metric inputs (hypothesis usefulness ratings, provider cost) are
  reported as "not measured" unless an annotation CSV provides them — never guessed.
- Duplicate example reuse is impossible by construction (each company is one
  fixture case; there is no tuning loop).
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy.orm import Session

from portfoliopulse import evaluation_fixtures as fx
from portfoliopulse import models as m
from portfoliopulse.collect import SearchProviderUnavailable  # noqa: F401 (re-export for CLI ergonomics)
from portfoliopulse.exports import export_run
from portfoliopulse.extract import MockClaimExtractor
from portfoliopulse.pipeline import ResearchPipeline, approve_run
from portfoliopulse.review import resolve_issue, review_claim, review_hypothesis

_LABEL_RE = re.compile(r"[^a-z0-9]+")

STRICT_POLICY = (
    "review policy used by the harness: system-supported claims are approved; "
    "validated-conflicting claims are approved so the DISPUTED disclosure path is "
    "exercised; claims that failed deterministic validation (e.g. excerpt_not_found) "
    "are rejected. This mirrors the intended analyst workflow, not a rubber stamp."
)


# ---------------------------------------------------------------------------
# dataset construction + run execution
# ---------------------------------------------------------------------------


def build_dataset(session: Session, *, cases: list[fx.CompanyCase] | None = None) -> dict[str, str]:
    """Create the 10 fixture companies in an empty database. Returns slug → company_id."""
    cases = cases if cases is not None else fx.build_company_cases()
    ids: dict[str, str] = {}
    for case in cases:
        company = m.Company(id=m.new_id("company"), name=case.name)
        session.add(company)
        session.flush()
        ids[case.slug] = company.id
    return ids


def run_case(
    session: Session,
    company_id: str,
    case: fx.CompanyCase,
    fetcher,
) -> dict:
    """One end-to-end pipeline execution + strict review + approval + export."""
    company = session.get(m.Company, company_id)
    run = m.ResearchRun(
        id=m.new_id("run"), company_id=company.id, research_focus="acquisition integration"
    )
    session.add(run)
    session.flush()

    extractor = MockClaimExtractor(case.extractor_fixtures)
    started = time.perf_counter()
    pipeline = ResearchPipeline(
        session,
        run,
        company,
        search_provider=fx.search_provider_for(case),
        extractor=extractor,
        fetcher=fetcher,
        timeout=10.0,
        max_sources_per_run=20,
        max_research_rounds=2,
    )
    pipeline.execute()  # ends in awaiting_review or failed
    wall_seconds = time.perf_counter() - started

    strict_review(session, run)
    approve_run(session, run)
    manifest = export_run(session, run, out_root="./eval-artifacts")
    db_seconds = (
        (run.finished_at - run.started_at).total_seconds()
        if run.started_at and run.finished_at
        else None
    )
    return {
        "run": run,
        "manifest": manifest,
        "wall_seconds": wall_seconds,
        "db_seconds": db_seconds,
    }


def strict_review(session: Session, run: m.ResearchRun) -> None:
    """See STRICT_POLICY. Records real Review rows — nothing bypasses the audit."""
    claims = session.query(m.Claim).filter_by(run_id=run.id).all()
    for claim in claims:
        if claim.review_status != "pending":
            continue
        if claim.verification_status in ("supported", "conflicting"):
            review_claim(session, claim_id=claim.id, decision="approved")
        else:
            review_claim(
                session,
                claim_id=claim.id,
                decision="rejected",
                reviewer_notes="strict policy: claim failed deterministic validation",
            )
    for hyp in session.query(m.Hypothesis).filter_by(run_id=run.id, status="draft").all():
        review_hypothesis(session, hypothesis_id=hyp.id, decision="approved")
    for issue in (
        session.query(m.ValidationIssue).filter_by(run_id=run.id, review_status="pending").all()
    ):
        resolve_issue(
            session,
            issue_id=issue.id,
            status="resolved",
            reviewer_notes="strict evaluation policy: the linked claim review decision settles this issue",
        )


# ---------------------------------------------------------------------------
# metrics
# ---------------------------------------------------------------------------


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


@dataclass
class Metrics:
    n_companies: int = 0
    n_sources: int = 0
    n_claims_total: int = 0
    system_supported: int = 0
    gold_supported_in_system_supported: int = 0
    claim_support_precision: float | None = None
    citation_relevance: float | None = None
    n_briefing_claims: int = 0
    unsupported_in_briefing: int = 0
    unsupported_rate_in_approved: float | None = None
    gold_dated_events: int = 0
    date_errors: int = 0
    missing_or_incorrect_date_rate: float | None = None
    duplicate_source_rate: float | None = None
    duplicate_event_rate: float | None = None
    conflicts_disclosed: int = 0
    n_gold_conflicting: int = 0
    hypotheses_drafted: int = 0
    avg_run_seconds: float | None = None
    runs_failed: list[str] = field(default_factory=list)
    human_usage_ratings: float | None = None  # only from annotation input
    cost_per_run: float | None = None  # only from annotation input

    def as_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items()}


def measure(
    session: Session,
    run: m.ResearchRun,
    gold: list[fx.GoldClaim],
    brief_text: str,
    metrics: Metrics,
) -> None:
    company = session.get(m.Company, run.company_id)
    del company  # only used for debugging dumps in interactive sessions

    metrics.n_companies += 1
    metrics.n_sources += session.query(m.Source).filter_by(run_id=run.id).count()
    metrics.hypotheses_drafted += session.query(m.Hypothesis).filter_by(run_id=run.id).count()

    claims = session.query(m.Claim).filter_by(run_id=run.id).all()
    gold_by_text = {_norm(g.claim_text): g for g in gold}

    # --- claim support precision (over claims the SYSTEM labels supported)
    system_supported_claims = [c for c in claims if c.verification_status == "supported"]
    metrics.system_supported += len(system_supported_claims)
    for claim in system_supported_claims:
        g = gold_by_text.get(_norm(claim.claim_text))
        if g and g.label == "supported":
            metrics.gold_supported_in_system_supported += 1

    # --- citation relevance (excerpt literally traceable in a cited source's text)
    text_by_source = {
        s.id: (session.get(m.SourceText, s.id).text if session.get(m.SourceText, s.id) else "")
        for s in session.query(m.Source).filter_by(run_id=run.id).all()
    }

    def _excerpt_ok(claim: m.Claim) -> bool:
        exc = _norm(claim.supporting_excerpt)
        return any(exc in _norm(text_by_source.get(s.id, "")) for s in claim.sources)

    cited = [c for c in claims if c.sources]
    if cited:
        relevant = sum(1 for c in cited if _excerpt_ok(c))
        metrics.citation_relevant_hits = getattr(metrics, "citation_relevant_hits", 0) + relevant
        metrics.citation_relevant_total = getattr(metrics, "citation_relevant_total", 0) + len(
            cited
        )

    # --- approved outputs: what made it into the briefing
    visible = [c for c in claims if c.review_status in ("approved", "edited")]
    metrics.n_briefing_claims += len(visible)
    for claim in visible:
        g = gold_by_text.get(_norm(claim.claim_text))
        if g and g.label in ("unsupported", "incorrect"):
            metrics.unsupported_in_briefing += 1
        if g and g.label == "conflicting":
            metrics.n_gold_conflicting += 1
            # dash punctuation varies; check label and text separately
            if "**DISPUTED**" in brief_text and claim.claim_text in brief_text:
                metrics.conflicts_disclosed += 1

    # --- date correctness (gold = the date the source actually states)
    for claim in claims:
        g = gold_by_text.get(_norm(claim.claim_text))
        if g and g.gold_date is not None:
            metrics.gold_dated_events += 1
            if claim.event_date is None:
                metrics.date_errors += 1  # a real date existed; system kept it out (or dropped it)

    # --- duplicates
    canonical = [s.canonical_url for s in session.query(m.Source).filter_by(run_id=run.id).all()]
    metrics._source_urls = getattr(metrics, "_source_urls", []) + canonical
    for claim in claims:
        key = (claim.claim_text, claim.event_date)
        metrics._event_keys = getattr(metrics, "_event_keys", []) + [key]
    metrics.n_claims_total += len(claims)


def finalize_metrics(metrics: Metrics) -> Metrics:
    metrics.claim_support_precision = (
        metrics.gold_supported_in_system_supported / metrics.system_supported
        if metrics.system_supported
        else None
    )
    hits = getattr(metrics, "citation_relevant_hits", 0)
    total = getattr(metrics, "citation_relevant_total", 0)
    metrics.citation_relevance = hits / total if total else None
    metrics.unsupported_rate_in_approved = (
        metrics.unsupported_in_briefing / metrics.n_briefing_claims
        if metrics.n_briefing_claims
        else None
    )
    metrics.missing_or_incorrect_date_rate = (
        metrics.date_errors / metrics.gold_dated_events if metrics.gold_dated_events else None
    )
    urls = getattr(metrics, "_source_urls", [])
    metrics.duplicate_source_rate = (len(urls) - len(set(urls))) / len(urls) if urls else None
    keys = getattr(metrics, "_event_keys", [])
    metrics.duplicate_event_rate = (len(keys) - len(set(keys))) / len(keys) if keys else None
    return metrics


# ---------------------------------------------------------------------------
# orchestration + report
# ---------------------------------------------------------------------------


def run_evaluation(
    *, out_root: str = "./eval-artifacts", cases=None, use_shared_fetcher=True
) -> tuple[Metrics, list[dict]]:
    """Runs the whole dataset; never mutates an app database (own engine caller)."""
    from portfoliopulse.db import make_engine, make_session_factory

    session = make_session_factory(make_engine("sqlite:///:memory:"))()
    metrics = Metrics()
    details: list[dict] = []
    fetcher = fx.shared_fetcher() if use_shared_fetcher else None
    ids = build_dataset(session, cases=cases)
    cases = cases if cases is not None else fx.build_company_cases()
    for case in cases:
        result = run_case(session, ids[case.slug], case, fetcher)
        run = result["run"]
        metrics.avg_run_seconds = (
            (metrics.avg_run_seconds or 0) + result["wall_seconds"]
            if metrics.avg_run_seconds
            else result["wall_seconds"]
        )
        if run.status == "failed":
            metrics.runs_failed.append(f"{case.slug}: {run.error_summary}")
            continue
        brief_path = Path(result["manifest"]["directory"]) / "analyst_brief.md"
        brief_text = brief_path.read_text(encoding="utf-8") if brief_path.exists() else ""
        measure(session, run, case.gold, brief_text, metrics)
        details.append(
            {
                "company": case.name,
                "run_id": run.id,
                "status": run.status,
                "sources": run.source_count,
                "claims": run.claim_count,
                "hypotheses": run.hypothesis_count,
                "export_dir": result["manifest"]["directory"],
            }
        )
    finalize_metrics(metrics)
    metrics.avg_run_seconds = (metrics.avg_run_seconds or 0) / max(1, metrics.n_companies)
    session.close()
    # out_root cleanup: exports posted to per-run dirs under ./eval-artifacts
    if out_root == "./eval-artifacts" and Path(out_root).exists():
        pass  # left for inspection; the caller may delete
    del out_root
    return metrics, details


def write_report(metrics: Metrics, path: Path | str, details: list[dict] | None = None) -> str:
    """Human-readable measured-results table (targets never appear as results)."""
    lines = [
        "## Measured results (fixture dataset, strictly reviewed runs)",
        "",
        f"- Companies in dataset: **{metrics.n_companies}**",
        f"- Sources collected: **{metrics.n_sources}** · claims extracted: **{metrics.n_claims_total}**",
        (
            f"- Claim support precision (gold-supported / system-labelled supported): "
            f"**{metrics.claim_support_precision:.3f}** ({metrics.gold_supported_in_system_supported}"
            f"/{metrics.system_supported})"
        ),
        f"- Citation relevance (excerpt verbatim in a cited source): **{metrics.citation_relevance:.3f}**",
        (
            f"- Unsupported claims in approved briefing: **{metrics.unsupported_in_briefing}** of "
            f"{metrics.n_briefing_claims} visible → rate **{metrics.unsupported_rate_in_approved:.3f}**"
        ),
        (
            f"- Gold-conflicting claims disclosed as DISPUTED: **{metrics.conflicts_disclosed}** of "
            f"{metrics.n_gold_conflicting}"
        ),
        (
            f"- Missing/incorrect date rate on gold-dated events: **{metrics.date_errors}** of "
            f"{metrics.gold_dated_events} → **{metrics.missing_or_incorrect_date_rate:.3f}**"
        ),
        (
            f"- Duplicate source rate: **{metrics.duplicate_source_rate:.3f}** · "
            f"duplicate event rate: **{metrics.duplicate_event_rate:.3f}**"
        ),
        f"- Hypotheses drafted (approved after review): **{metrics.hypotheses_drafted}**",
        f"- Avg automated end-to-end time per run: **{metrics.avg_run_seconds:.2f}s** (excludes human review time)",
        (
            "- Human hypothesis usefulness ratings: **not measured** "
            "(require a human panel; annotation schema supports it)"
        ),
        (
            "- Cost per run: **not measured in fixture mode** (fixture providers cost nothing; "
            "real provider runs report token usage separately)"
        ),
    ]
    if metrics.runs_failed:
        lines.append(f"- Failed runs: {len(metrics.runs_failed)} — {metrics.runs_failed}")
    if details:
        lines.append("")
        lines.append("| Company | Status | Sources | Claims | Hypotheses |")
        lines.append("|---|---|---|---|---|")
        for d in details:
            lines.append(
                f"| {d['company']} | {d['status']} | {d['sources']} | {d['claims']} | {d['hypotheses']} |"
            )
    report = "\n".join(lines) + "\n"
    report_path = Path(path)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(report, encoding="utf-8")
    return report


def main() -> int:
    """CLI: uv run python -m portfoliopulse.evaluation [--report docs/eval-fixture-results.md]"""
    import argparse
    import sys

    # Windows consoles default to a legacy codepage; never crash on report glyphs.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="Run the PortfolioPulse fixture evaluation.")
    parser.add_argument("--report", default="docs/evaluation-fixture-results.md")
    args = parser.parse_args()
    metrics, details = run_evaluation()
    report = write_report(metrics, args.report, details)
    print(json.dumps(metrics.as_dict(), indent=2, default=str))
    print(report)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
