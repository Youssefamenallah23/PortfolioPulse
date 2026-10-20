"""Run orchestration: an explicit, typed state machine over the Step 1-5 components.

Design notes
- Nodes are ordinary functions/methods, run strictly sequentially. Transitions and
  statuses are explicit; every node persists its work (`session.flush`) so a crash
  between stages does not erase the run.
- All nodes are idempotent: source dedupe (Step 3), extraction skip-already-done
  (Step 4), unvalidated-claims recompute (Step 5), hypothesis skip-already-done.
- Evidence gaps trigger AT MOST `max_research_rounds` bounded collection rounds,
  then the run ends honestly with a gap report in warnings — never an infinite loop.
- Nothing reaches `approved` without persisted human review (see `approve_run`).

# ponytail: plain state machine, not LangGraph — same explicit-transition contract
# with zero new dependencies. Swapping in LangGraph later is a mechanical adapter
# over these same node methods; do it when checkpointing/interrupts are actually needed.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from portfoliopulse import models as m
from portfoliopulse.collect import (
    MAX_BODY_BYTES,
    DomainThrottle,
    PageFetcher,
    SearchProvider,
    UrlTarget,
    canonical_url,
    collect_targets,
)
from portfoliopulse.db import utcnow
from portfoliopulse.extract import ClaimExtractor, extract_run_sources
from portfoliopulse.hypotheses import MIN_SUPPORTED_CLAIMS, create_draft_hypotheses
from portfoliopulse.validate import ValidationReport, validate_run_claims

STATUS_RUNNING = "running"
STATUS_AWAITING_REVIEW = "awaiting_review"
STATUS_FAILED = "failed"
STATUS_APPROVED = "approved"


class ApprovalBlocked(RuntimeError):
    """Raised when a run cannot be approved for export — with an actionable reason."""


@dataclass
class PipelineState:
    """Typed run state; single owner of in-flight pipeline knowledge."""

    run_id: str
    company_id: str
    company_name: str
    website: str | None
    research_focus: str

    round: int = 0  # completed collect rounds
    plan: list[UrlTarget] = field(default_factory=list)
    plan_seen: set[str] = field(default_factory=set)
    validation: ValidationReport | None = None
    warnings: list[str] = field(default_factory=list)
    trace: list[str] = field(default_factory=list)
    status: str = STATUS_RUNNING
    error: str | None = None
    summary: dict[str, object] = field(default_factory=dict)

    def collect_fetched_source_count(self, session: Session) -> int:
        return session.query(m.Source).filter_by(run_id=self.run_id, status="fetched").count()


class ResearchPipeline:
    def __init__(
        self,
        session: Session,
        run: m.ResearchRun,
        company: m.Company,
        *,
        search_provider: SearchProvider,
        extractor: ClaimExtractor,
        fetcher: PageFetcher,
        throttle: DomainThrottle | None = None,
        timeout: float = 15.0,
        max_sources_per_run: int = 20,
        max_research_rounds: int = 2,
        min_supported_claims: int = MIN_SUPPORTED_CLAIMS,
        max_body_bytes: int = MAX_BODY_BYTES,
    ):
        if max_research_rounds < 1:
            raise ValueError("max_research_rounds must be >= 1")
        self.session = session
        self.run = run
        self.company = company
        self.search_provider = search_provider
        self.extractor = extractor
        self.fetcher = fetcher
        self.throttle = throttle
        self.timeout = timeout
        self.max_sources_per_run = max_sources_per_run
        self.max_research_rounds = max_research_rounds
        self.min_supported_claims = min_supported_claims
        self.max_body_bytes = max_body_bytes

        self.state = PipelineState(
            run_id=run.id,
            company_id=company.id,
            company_name=company.name,
            website=company.website,
            research_focus=run.research_focus,
        )

    # ------------------------------------------------------------------ helpers

    def _trace(self, node: str) -> None:
        self.state.trace.append(node)

    def _fail(self, error: str) -> None:
        self.state.status = STATUS_FAILED
        self.state.error = error
        self.run.status = STATUS_FAILED
        self.run.error_summary = error
        self.run.finished_at = utcnow()
        self.session.flush()
        self._trace("failed")

    def _record_config(self) -> None:
        self.run.config_summary = {
            "search_provider": getattr(self.search_provider, "provider_name", "custom"),
            "llm_provider": getattr(self.extractor, "provider_name", "custom"),
            "max_sources_per_run": self.max_sources_per_run,
            "max_research_rounds": self.max_research_rounds,
        }
        self.session.flush()

    # ------------------------------------------------------------------- nodes

    def _node_validate_request(self) -> bool:
        self._trace("validate_request")
        if not (self.company.name or "").strip():
            self._fail("company name is empty; a research run needs a subject")
            return False
        if not (self.run.research_focus or "").strip():
            self._fail("research focus is empty; a research run needs a scope")
            return False
        if self.company.website:
            try:
                canonical_url(self.company.website)
            except ValueError as exc:
                self._fail(f"official website URL is not usable: {exc}")
                return False
        self.run.status = STATUS_RUNNING
        self.run.started_at = self.run.started_at or utcnow()
        self._record_config()
        self.session.flush()
        return True

    def _plan_from(self, targets: list[UrlTarget], *, follow_up: bool = False) -> None:
        """Extends the plan, dropping canonical URLs seen before (idempotency)."""
        added = 0
        for target in targets:
            try:
                key = canonical_url(target.url)
            except ValueError:
                continue  # invalid URLs are recorded at fetch time, not planned
            if key in self.state.plan_seen:
                continue
            self.state.plan_seen.add(key)
            self.state.plan.append(target)
            added += 1
        if follow_up and added == 0:
            self.state.warnings.append("follow-up search produced no new sources")

    def _node_plan_research(self, follow_up: bool = False) -> bool:
        self._trace("plan_research" + ("_followup" if follow_up else ""))
        try:
            hits = self.search_provider.search(
                self.company.name, self.run.research_focus, limit=self.max_sources_per_run
            )
        except Exception as exc:  # noqa: BLE001 - provider misconfiguration must stop the run, loudly
            self._fail(f"research planning failed: provider error: {exc}")
            return False
        targets = [
            UrlTarget(url=hit.url, title=hit.title, source_type=hit.source_type or "search_result")
            for hit in hits
        ]
        if not follow_up and self.company.website:
            targets.insert(
                0,
                UrlTarget(
                    url=self.company.website, title="Company site", source_type="official_page"
                ),
            )
        self._plan_from(targets, follow_up=follow_up)
        return True

    def _node_collect_sources(self) -> bool:
        self.state.round += 1
        report = collect_targets(
            self.session,
            self.run,
            self.company,
            self.state.plan,
            fetcher=self.fetcher,
            timeout=self.timeout,
            throttle=self.throttle,
            max_sources_per_run=self.max_sources_per_run,
            max_bytes=self.max_body_bytes,
        )
        totals = report.totals()
        self.state.warnings.append(f"round {self.state.round} collection: {totals}")
        self._trace(f"collect_sources(round={self.state.round}, {totals})")
        if self.state.collect_fetched_source_count(self.session) == 0:
            self._fail("no usable sources found in permitted public sources for this request")
            return False
        return True

    def _node_extract_claims(self) -> bool:
        results = extract_run_sources(
            self.session, self.run, extractor=self.extractor, focus=self.run.research_focus
        )
        for result in results:
            self.state.warnings.extend(f"extraction: {w}" for w in result.warnings)
        extracted = sum(1 for r in results if r.status == "extracted")
        self._trace(f"extract_claims(sources_ok={extracted}/{len(results)})")
        return True

    def _node_validate_claims(self) -> bool:
        report = validate_run_claims(self.session, self.run)
        self.state.validation = report
        self._trace(f"validate_claims({report.totals()})")
        return True

    def _node_generate_hypotheses(self) -> list[m.Hypothesis]:
        hypotheses, warnings = create_draft_hypotheses(self.session, self.run)
        self.state.warnings.extend(f"hypotheses: {w}" for w in warnings)
        self._trace(f"generate_hypotheses({len(hypotheses)})")
        return hypotheses

    def _node_review_pending(self) -> None:
        """Terminal status for a complete-but-unreviewed run. Exports stay blocked."""
        pending_claims = (
            self.session.query(m.Claim)
            .filter_by(run_id=self.run.id, review_status="pending")
            .count()
        )
        pending_issues = (
            self.session.query(m.ValidationIssue)
            .filter_by(run_id=self.run.id, review_status="pending")
            .count()
        )
        self.run.status = STATUS_AWAITING_REVIEW
        self.run.finished_at = utcnow()
        self.session.flush()
        self.state.warnings.append(
            f"{pending_claims} claims need analyst review; {pending_issues} validation "
            "issues open; no approved report has been generated."
        )
        self._trace("review_pending")

    # -------------------------------------------------------------------- loop

    def execute(self) -> PipelineState:
        self._trace("start")
        if not self._node_validate_request():
            return self.state

        if not self._node_plan_research():
            return self.state

        while True:
            try:
                if not self._node_collect_sources():
                    break
                self._node_extract_claims()
                self._node_validate_claims()
            except Exception as exc:  # noqa: BLE001 - node failure boundary: record + fail the run
                self._fail(f"pipeline node failed: {type(exc).__name__}: {exc}")
                break
            supported = self.state.validation.supported if self.state.validation else 0
            if supported >= self.min_supported_claims:
                break
            if self.state.round >= self.max_research_rounds:
                self.state.warnings.append(
                    f"max research rounds ({self.max_research_rounds}) reached with "
                    f"{supported} supported claim(s); recorded as evidence gaps"
                )
                break
            self.state.warnings.append(
                f"insufficient supported evidence ({supported}) after round "
                f"{self.state.round}; planning bounded follow-up"
            )
            if not self._node_plan_research(follow_up=True):
                break

        if self.state.status != STATUS_FAILED:
            self._node_generate_hypotheses()
            self._node_review_pending()
        self._finish()
        return self.state

    def _finish(self) -> None:
        """Refresh denormalised counters and compute the honest end-of-run summary."""
        if self.run.status == STATUS_RUNNING:
            self._node_review_pending()  # safety net; normally already set
        self.run.source_count = self.session.query(m.Source).filter_by(run_id=self.run.id).count()
        self.run.claim_count = self.session.query(m.Claim).filter_by(run_id=self.run.id).count()
        self.run.hypothesis_count = (
            self.session.query(m.Hypothesis).filter_by(run_id=self.run.id).count()
        )
        validation = self.state.validation
        draft_hypotheses = (
            self.session.query(m.Hypothesis).filter_by(run_id=self.run.id, status="draft").count()
        )
        self.state.summary = {
            "sources_collected": self.run.source_count,
            "claims_extracted": self.run.claim_count,
            "claims_supported": validation.supported if validation else 0,
            "claims_needing_review": validation.needs_review if validation else 0,
            "open_conflicts": validation.conflicting if validation else 0,
            "hypotheses_drafted": draft_hypotheses,
            "exports": [],
            "next_action": "Review claims and draft hypotheses before approving exports.",
        }
        self.session.flush()
        self._trace("finalize")


def approve_run(session: Session, run: m.ResearchRun) -> m.ResearchRun:
    """Approve a run for export — only after human review is fully recorded.

    Criteria (each checked from persisted state):
      1. No claims left in `pending` review status.
      2. No draft hypotheses left (each must be approved/edited or rejected).
      3. No pending validation issues.
    """
    if run.status == STATUS_FAILED:
        raise ApprovalBlocked("a failed run cannot be approved")
    pending_claims = (
        session.query(m.Claim).filter_by(run_id=run.id, review_status="pending").count()
    )
    if pending_claims:
        raise ApprovalBlocked(f"{pending_claims} claim(s) still await review")
    pending_hypotheses = (
        session.query(m.Hypothesis).filter_by(run_id=run.id, status="draft").count()
    )
    if pending_hypotheses:
        raise ApprovalBlocked(f"{pending_hypotheses} draft hypothesis/ies still await review")
    pending_issues = (
        session.query(m.ValidationIssue).filter_by(run_id=run.id, review_status="pending").count()
    )
    if pending_issues:
        raise ApprovalBlocked(f"{pending_issues} validation issue(s) still pending")
    run.status = STATUS_APPROVED
    run.finished_at = utcnow()
    session.flush()
    return run
