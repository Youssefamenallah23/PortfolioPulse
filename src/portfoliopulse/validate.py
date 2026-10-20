"""Evidence validation: claims must be provable, not just plausible.

Deterministic only (reproducible, unit-tested). An optional LLM entailment check
may be added later as model-assisted review — it can never REPLACE these checks.

Rules implemented:
1. Every claim must link to at least one source record.              -> missing_source
2. Fetched sources must carry retrieval metadata.                    -> missing_source_metadata
3. The supporting excerpt must literally occur in the stored text    -> excerpt_not_found
   of at least one linked source.
4. A claim must not say more than its proof: every number in the     -> claim_overstates_excerpt
   claim text must be present in the excerpt or the stored text.
5. An event dated AFTER the source existed is suspicious unless      -> date_confusion
   the claim text signals future intent ("plans to", "by 2027", ...).
6. Claims from different sources that disagree (same category,
   divergent figures or event dates, lexically overlapping) are      -> conflicting_claims
   BOTH marked conflicting; no winner is chosen. The analyst decides.

Verification status lifecycle (Claim.verification_status):
unverified → supported (all checks pass) | conflicting (in a conflict pair)
"unsupported" is reserved for human rejection, never set automatically.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from portfoliopulse import models as m

ISSUE_MISSING_SOURCE = "missing_source"
ISSUE_MISSING_METADATA = "missing_source_metadata"
ISSUE_SOURCE_TEXT_MISSING = "source_text_missing"
ISSUE_EXCERPT_NOT_FOUND = "excerpt_not_found"
ISSUE_CLAIM_OVERSTATES = "claim_overstates_excerpt"
ISSUE_DATE_CONFUSION = "date_confusion"
ISSUE_CONFLICTING = "conflicting_claims"

# Qualifiers that mark a claim as future-looking: its event date legitimately
# postdates the source publication/retrieval.
_FUTURE_RE = re.compile(
    r"(plans? to|expects? to|intends? to|aims? to|seeks? to|will\b|scheduled|expected|"
    r"forecast|projected|aims|\bby\s+\d{4}|\bduring\s+\d{4}|\bin\s+20\d\d)",
    re.IGNORECASE,
)
_NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)*(?:%|\s*(?:million|billion|bn|mn))?", re.IGNORECASE)
_TOKEN_RE = re.compile(r"[a-z\u00C0-\u017F]{3,}")


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip().lower()


def _numbers(text: str) -> set[str]:
    return {n.strip().rstrip("%").rstrip(".") for n in _NUMBER_RE.findall(text or "")}


def _tokens(text: str) -> set[str]:
    return set(_TOKEN_RE.findall((text or "").lower()))


def _overlap(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def are_contradictory(a: m.Claim, b: m.Claim) -> str | None:
    """Deterministic conflict classifier between two claims.

    Returns "figures" | "dates" when they plausibly disagree, else None.
    Conservative by design: same category + lexical overlap required, so unrelated
    claims are never auto-flagged. Borderline cases surface to the analyst.
    """
    if a.id == b.id or a.category != b.category:
        return None
    tokens_a, tokens_b = _tokens(a.claim_text), _tokens(b.claim_text)
    if _overlap(tokens_a, tokens_b) < 0.25:
        return None
    na, nb = _numbers(a.claim_text), _numbers(b.claim_text)
    if na and nb and not (na & nb):
        return "figures"
    if a.event_date and b.event_date and a.event_date != b.event_date:
        return "dates"
    return None


@dataclass
class ValidationReport:
    supported: int = 0
    needs_review: int = 0
    conflicting: int = 0
    issues: list[m.ValidationIssue] = field(default_factory=list)

    def totals(self) -> dict[str, int]:
        return {
            "supported": self.supported,
            "needs_review": self.needs_review,
            "conflicting": self.conflicting,
            "issues": len(self.issues),
        }


def _add_issue(
    session: Session,
    run_id: str,
    issue_type: str,
    message: str,
    claim_id: str | None = None,
    related_claim_id: str | None = None,
) -> m.ValidationIssue:
    issue = m.ValidationIssue(
        id=m.new_id("vi"),
        run_id=run_id,
        issue_type=issue_type,
        message=message,
        claim_id=claim_id,
        related_claim_id=related_claim_id,
    )
    session.add(issue)
    return issue


def _claim_issues(
    session: Session, run_id: str, claim: m.Claim, text_by_source: dict[str, str]
) -> list[m.ValidationIssue]:
    issues: list[m.ValidationIssue] = []

    if not claim.sources:
        issues.append(
            _add_issue(
                session,
                run_id,
                ISSUE_MISSING_SOURCE,
                f"claim {claim.id} has no linked source and cannot be verified.",
                claim_id=claim.id,
            )
        )
        return issues

    for source in claim.sources:
        if source.status == "fetched" and source.retrieved_at is None:
            issues.append(
                _add_issue(
                    session,
                    run_id,
                    ISSUE_MISSING_METADATA,
                    f"source {source.id} is marked fetched but has no retrieval timestamp.",
                    claim_id=claim.id,
                )
            )

    source_texts = [text_by_source[s.id] for s in claim.sources if s.id in text_by_source]
    if not source_texts:
        issues.append(
            _add_issue(
                session,
                run_id,
                ISSUE_SOURCE_TEXT_MISSING,
                f"stored text missing for every linked source; claim {claim.id} cannot be checked.",
                claim_id=claim.id,
            )
        )
        return issues

    excerpt_norm = _norm(claim.supporting_excerpt)
    if excerpt_norm and not any(excerpt_norm in _norm(text) for text in source_texts):
        issues.append(
            _add_issue(
                session,
                run_id,
                ISSUE_EXCERPT_NOT_FOUND,
                f"supporting excerpt of claim {claim.id} was not found in any linked source text.",
                claim_id=claim.id,
            )
        )

    # A claim may not contain a figure its evidence does not contain anywhere.
    haystack = _norm(claim.supporting_excerpt) + " " + " ".join(_norm(t) for t in source_texts)
    haystack_nums = _numbers(haystack)
    unsupported_nums = _numbers(claim.claim_text) - haystack_nums
    if unsupported_nums:
        issues.append(
            _add_issue(
                session,
                run_id,
                ISSUE_CLAIM_OVERSTATES,
                f"claim {claim.id} contains figure(s) {sorted(unsupported_nums)} not present in its evidence.",
                claim_id=claim.id,
            )
        )

    # Publication/event date confusion (unless the text itself says "future").
    if claim.event_date is not None and not _FUTURE_RE.search(claim.claim_text):
        anchors = [s.published_at.date() for s in claim.sources if s.published_at]
        anchors += [s.retrieved_at.date() for s in claim.sources if s.retrieved_at]
        if anchors and claim.event_date > max(anchors):
            issues.append(
                _add_issue(
                    session,
                    run_id,
                    ISSUE_DATE_CONFUSION,
                    f"claim {claim.id} asserts an event dated {claim.event_date}, after its sources existed, "
                    "and does not signal future intent. Possible publication/event date confusion.",
                    claim_id=claim.id,
                )
            )
    return issues


def validate_run_claims(session: Session, run: m.ResearchRun) -> ValidationReport:
    """Validate all claims of a run. Fresh issues are created per call inside one
    transaction; the caller decides commit points. Status is recomputed, never trusted."""
    report = ValidationReport()
    claims = session.query(m.Claim).filter_by(run_id=run.id).all()

    text_by_source: dict[str, str] = {}
    for source in session.query(m.Source).filter_by(run_id=run.id).all():
        stored = session.get(m.SourceText, source.id)
        if stored:
            text_by_source[source.id] = stored.text

    per_claim_issues: dict[str, list[str]] = {}

    for claim in claims:
        issues = _claim_issues(session, run.id, claim, text_by_source)
        per_claim_issues[claim.id] = [i.issue_type for i in issues]
        claim.verification_status = "supported" if not issues else "unverified"
        report.issues.extend(issues)

    # Pairwise contradiction pass — same-category claims from different evidence.
    for i in range(len(claims)):
        for j in range(i + 1, len(claims)):
            a, b = claims[i], claims[j]
            kind = are_contradictory(a, b)
            if kind:
                pair_issue = _add_issue(
                    session,
                    run.id,
                    ISSUE_CONFLICTING,
                    f"claims {a.id} and {b.id} disagree on {kind} while covering the same topic; "
                    "the analyst decides which to trust.",
                    claim_id=a.id,
                    related_claim_id=b.id,
                )
                report.issues.append(pair_issue)
                a.verification_status = b.verification_status = "conflicting"

    for claim in claims:
        if claim.verification_status == "supported":
            report.supported += 1
        elif claim.verification_status == "conflicting":
            report.conflicting += 1
        else:
            report.needs_review += 1

    session.flush()
    return report


def open_issues_for_claim(session: Session, claim_id: str) -> list[m.ValidationIssue]:
    """Everything still unresolved about a claim — the review UI consumes this."""
    return (
        session.query(m.ValidationIssue)
        .filter(
            (m.ValidationIssue.claim_id == claim_id)
            | (m.ValidationIssue.related_claim_id == claim_id)
        )
        .filter(m.ValidationIssue.review_status == "pending")
        .order_by(m.ValidationIssue.created_at)
        .all()
    )
