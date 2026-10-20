"""Human review workflow — the analyst layer between extraction and export.

Rules
- Every decision becomes a persisted, auditable Review row (models.record_review).
- Malformed requests and unknown targets raise typed errors (mapped to 4xx in the
  API). Unknown targets are ReviewNotFoundError (404), wrong shape ReviewInputError.
- "flagged" records the analyst's disagreement WITHOUT deciding a conflict outcome —
  the claim stays `pending` until a human changes it.
- Rejected material stays in the audit trail; `visible_claims()` is the SINGLE filter
  exports (Step 9) must use, so rejected claims can never reappear as established facts.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from portfoliopulse import models as m

DECISIONS = ("approved", "edited", "rejected", "flagged")
ISSUE_STATUSES = ("resolved", "waived")


class ReviewInputError(ValueError):
    """Malformed review request (payload semantics) — API maps to 400."""


class ReviewNotFoundError(ValueError):
    """Unknown target id — API maps to 404."""


def review_claim(
    session: Session,
    *,
    claim_id: str,
    decision: str,
    reviewer_notes: str | None = None,
    edited_text: str | None = None,
) -> m.Review:
    if decision not in DECISIONS:
        raise ReviewInputError(f"decision must be one of {DECISIONS}")
    claim = session.get(m.Claim, claim_id)
    if claim is None:
        raise ReviewNotFoundError(f"claim {claim_id!r} does not exist")
    edited_value = _require_edit_payload(decision, edited_text, "claim_text")
    return m.record_review(
        session,
        target_type="claim",
        target_id=claim_id,
        decision=decision,
        reviewer_notes=reviewer_notes,
        edited_value=edited_value,
    )


def review_hypothesis(
    session: Session,
    *,
    hypothesis_id: str,
    decision: str,
    reviewer_notes: str | None = None,
    edited_text: str | None = None,
) -> m.Review:
    if decision not in DECISIONS:
        raise ReviewInputError(f"decision must be one of {DECISIONS}")
    hypothesis = session.get(m.Hypothesis, hypothesis_id)
    if hypothesis is None:
        raise ReviewNotFoundError(f"hypothesis {hypothesis_id!r} does not exist")
    edited_value = _require_edit_payload(decision, edited_text, "hypothesis_text")
    return m.record_review(
        session,
        target_type="hypothesis",
        target_id=hypothesis_id,
        decision=decision,
        reviewer_notes=reviewer_notes,
        edited_value=edited_value,
    )


def _require_edit_payload(
    decision: str, edited_text: str | None, field: str
) -> dict[str, str] | None:
    if decision == "edited":
        if not (edited_text or "").strip():
            raise ReviewInputError(f"decision 'edited' requires a non-empty edited_text ({field})")
        return {field: edited_text}
    if edited_text is not None:
        raise ReviewInputError("edited_text is only valid with decision 'edited'")
    return None


def resolve_issue(
    session: Session,
    *,
    issue_id: str,
    status: str,
    reviewer_notes: str | None = None,
) -> m.ValidationIssue:
    """Mark a validation issue resolved / waived (never auto: a human decides)."""
    issue = session.get(m.ValidationIssue, issue_id)
    if issue is None:
        raise ReviewNotFoundError(f"validation issue {issue_id!r} does not exist")
    if status not in ISSUE_STATUSES:
        raise ReviewInputError(f"status must be one of {ISSUE_STATUSES}")
    issue.review_status = status
    # full audit trail: the issue keeps its own status; the Review row keeps who/why
    session.add(
        m.Review(
            id=m.new_id("rev"),
            target_type="validation_issue",
            target_id=issue_id,
            decision=status,
            reviewer_notes=reviewer_notes,
        )
    )
    session.flush()
    return issue


def unresolved_summary(session: Session, run: m.ResearchRun) -> dict[str, int]:
    """Counts the approval gate depends on; mirrored by the review payload endpoint."""
    return {
        "pending_claims": session.query(m.Claim)
        .filter_by(run_id=run.id, review_status="pending")
        .count(),
        "open_hypotheses": session.query(m.Hypothesis)
        .filter_by(run_id=run.id, status="draft")
        .count(),
        "pending_issues": session.query(m.ValidationIssue)
        .filter_by(run_id=run.id, review_status="pending")
        .count(),
    }


def visible_claims(session: Session, run: m.ResearchRun) -> list[m.Claim]:
    """The single filter for exports/conclusions: rejected claims are excluded.

    They remain fully available in the audit trail (Review rows, claim rows).
    """
    return (
        session.query(m.Claim)
        .filter_by(run_id=run.id)
        .filter(m.Claim.review_status != "rejected")
        .all()
    )
