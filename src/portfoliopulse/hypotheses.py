"""Draft hypothesis generation: preliminary, cautious, evidence-linked or nothing.

Rules (enforced here, tested in Step 7):
- Only SUPPORTED claims may feed a hypothesis; exceptions must be explicitly flagged.
- Fewer than 2 supported claims → no hypothesis + "insufficient evidence" warning.
- Every hypothesis carries alternative explanations, missing evidence and concrete
  validation questions. It is a question to investigate, never a finding.
- Idempotent: hypotheses already exist for the run → generation skipped.
- Language stays prefliminary ("may", "potentially"); the pipeline gates approval.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from portfoliopulse import models as m

MAX_HYPOTHESES_PER_RUN = 3
MIN_SUPPORTED_CLAIMS = 2


@dataclass(frozen=True)
class HypothesisPattern:
    theme: str
    title: str
    statement: str
    alternatives: tuple[str, ...]
    missing_evidence: tuple[str, ...]
    questions: tuple[str, ...]


# ponytail: fixed template patterns per evidence theme — replace/augment with an
# LLM synthesis call once a real provider exists; the deterministic baseline stays
# the test oracle either way.
PATTERNS: dict[str, HypothesisPattern] = {
    "acquisition": HypothesisPattern(
        theme="acquisition",
        title="Potential integration complexity",
        statement="the volume of acquisition activity may be increasing coordination "
        "demands across business units.",
        alternatives=(
            "A mature integration function may already handle this well.",
            "The acquired businesses may be intended to operate independently.",
        ),
        missing_evidence=(
            "Current operating model, decision rights and reporting lines",
            "Integration governance structures and performance measures",
        ),
        questions=(
            "Who owns cross-business integration decisions?",
            "How are responsibilities assigned after acquisitions, and what is measured?",
        ),
    ),
    "geographic_expansion": HypothesisPattern(
        theme="geographic_expansion",
        title="Potential expansion coordination strain",
        statement="the pattern of geographic expansion may be straining existing "
        "coordinating mechanisms.",
        alternatives=(
            "The company may operate regions through a decentralised design by choice.",
            "The expansions may be small-scale relative to the existing footprint.",
        ),
        missing_evidence=(
            "Operating footprint by region and decision-making boundaries",
            "How regional units coordinate with head-office functions",
        ),
        questions=(
            "Which decisions are centralised versus delegated to regions?",
            "What indicators track coordination performance across regions?",
        ),
    ),
    "leadership": HypothesisPattern(
        theme="leadership",
        title="Potential governance restructuring",
        statement="the pattern of leadership changes may indicate restructuring of "
        "decision rights.",
        alternatives=(
            "Changes may reflect planned succession rather than restructuring.",
            "Role changes may redistribute work without moving decisions.",
        ),
        missing_evidence=(
            "Current governance chart and committee structures",
            "Reasons stated for each change",
        ),
        questions=(
            "What decision rights moved with the leadership changes?",
            "Have formal governance bodies been re-established?",
        ),
    ),
    "strategy": HypothesisPattern(
        theme="strategy",
        title="Potential operating-model strain",
        statement="the strategy signals may be outpacing the current operating model.",
        alternatives=(
            "The company may be maturing the model in parallel with the shift.",
            "The shift may be smaller in practice than announcements suggest.",
        ),
        missing_evidence=(
            "Operating model documentation and ownership boundaries",
            "Evidence of implementation progress versus plan",
        ),
        questions=(
            "How are cross-unit responsibilities coordinated today?",
            "What would failure of the shift look like, and who would see it first?",
        ),
    ),
}

# Claim category → evidence theme driving pattern selection.
THEME_BY_CATEGORY = defaultdict(
    lambda: "strategy",
    {
        "acquisition": "acquisition",
        "geographic_expansion": "geographic_expansion",
        "leadership": "leadership",
        "strategy": "strategy",
        "company_profile": "strategy",
        "ownership": "strategy",
        "hiring_signal": "strategy",
        "industry_context": "strategy",
    },
)


def create_draft_hypotheses(
    session: Session, run: m.ResearchRun, *, max_count: int = MAX_HYPOTHESES_PER_RUN
) -> tuple[list[m.Hypothesis], list[str]]:
    """Create draft hypotheses from SUPPORTED claims only. Returns (hypotheses, warnings)."""
    warnings: list[str] = []

    existing = session.query(m.Hypothesis).filter_by(run_id=run.id).count()
    if existing:
        return [], [f"{existing} hypotheses already exist for this run; generation skipped"]

    supported = (
        session.query(m.Claim)
        .filter_by(run_id=run.id, verification_status="supported")
        .order_by(m.Claim.created_at)
        .all()
    )
    if len(supported) < MIN_SUPPORTED_CLAIMS:
        warning = (
            "insufficient supported evidence to form a useful preliminary hypothesis "
            f"({len(supported)} supported claim(s) need at least {MIN_SUPPORTED_CLAIMS})"
        )
        return [], [warning]

    themes: dict[str, list[m.Claim]] = defaultdict(list)
    for claim in supported:
        themes[THEME_BY_CATEGORY[claim.category]].append(claim)

    # Build → validate → persist. A draft failing its own quality gate is never stored.
    built: list[m.Hypothesis] = []
    for theme, claims in themes.items():
        if len(claims) < MIN_SUPPORTED_CLAIMS:
            continue
        pattern = PATTERNS[theme]
        hypothesis_text = (
            f"DRAFT — Observed {len(claims)} supported claims suggest: {pattern.statement}"
        )
        issues = validate_hypothesis_draft(
            session,
            run_id=run.id,
            title=pattern.title,
            hypothesis_text=hypothesis_text,
            alternative_explanations=list(pattern.alternatives),
            missing_evidence=list(pattern.missing_evidence),
            questions_to_validate=list(pattern.questions),
            supporting_claim_ids=[claim.id for claim in claims],
        )
        if issues:  # defense in depth: templates must never drift into overstatement
            warnings.append(f"unsafe draft rejected before persisting: {'; '.join(issues)}")
            continue

        hypothesis = m.Hypothesis(
            id=m.new_id("hyp"),
            run_id=run.id,
            company_id=run.company_id,
            title=pattern.title,
            hypothesis_text=hypothesis_text,
            alternative_explanations=list(pattern.alternatives),
            missing_evidence=list(pattern.missing_evidence),
            questions_to_validate=list(pattern.questions),
            evidence_summary=_evidence_summary(pattern, claims),
            status="draft",
        )
        session.add(hypothesis)
        hypothesis.claims.extend(claims)  # ORM-managed junction inserts
        built.append(hypothesis)
        if len(built) >= max_count:
            break

    if not built:
        warnings.append(
            "supported claims exist but are too thematically isolated to form a "
            "preliminary hypothesis; review the evidence first"
        )

    run.hypothesis_count = session.query(m.Hypothesis).filter_by(run_id=run.id).count()
    session.flush()
    return built, warnings


def _evidence_summary(pattern: HypothesisPattern, claims: list[m.Claim]) -> dict[str, Any]:
    """Direct facts (claim + its source URLs) kept strictly apart from interpretation."""
    direct_facts: list[dict[str, Any]] = []
    for claim in claims:
        session_sources = claim.sources
        direct_facts.append(
            {
                "claim_id": claim.id,
                "claim_text": claim.claim_text,
                "source_urls": sorted(getattr(s, "url", "") or "" for s in session_sources),
            }
        )
    return {
        "direct_facts": direct_facts,
        "interpretation": {
            "text": pattern.statement,
            "kind": "inference",
            "note": (
                "A template-based reading of the direct facts above. It is itself "
                "NOT a fact and must be tested by a human before use."
            ),
        },
    }


DEFINITIVE_MARKERS = (
    "inefficient",
    "dysfunctional",
    "failing",
    "poor management",
    "proven",
    "confirmed that",
    "definitely",
    "certainly",
    "clearly",
    "unquestionably",
    "obviously",
    "we recommend",
    "must immediately",
    "should immediately",
    "will certainly",
)

HEDGES = ("may", "might", "potentially", "possibly")


def validate_hypothesis_draft(
    session: Session,
    *,
    run_id: str | None = None,
    title: str = "",
    hypothesis_text: str,
    alternative_explanations: list,
    missing_evidence: list,
    questions_to_validate: list,
    supporting_claim_ids: list[str] | None = None,
) -> list[str]:
    """Deterministic Step 7 quality gate. An empty list == safe to store."""
    issues: list[str] = []
    if not title.strip():
        issues.append("missing title")
    if not hypothesis_text.strip():
        issues.append("missing hypothesis text")
    if not alternative_explanations:
        issues.append("missing alternative explanation (at least one is required)")
    if not missing_evidence:
        issues.append("missing evidence list is empty (what is needed to test it)")
    if not questions_to_validate:
        issues.append("no concrete validation questions")

    lowered = hypothesis_text.lower()
    if not any(hedge in lowered for hedge in HEDGES):
        issues.append(
            "phrases an inference like a proven fact: no preliminary hedge (may/might) present"
        )
    for marker in DEFINITIVE_MARKERS:
        if marker in lowered:
            issues.append(f"definitive/overstated language: {marker!r}")

    if supporting_claim_ids:
        query = session.query(m.Claim).filter(m.Claim.id.in_(supporting_claim_ids))
        if run_id is not None:
            query = query.filter_by(run_id=run_id)
        known = {row.id for row in query.all()}
        unknown = sorted(set(supporting_claim_ids) - known)
        if unknown:
            issues.append(
                f"references {len(unknown)} nonexistent claim id(s) for this run: {unknown}"
            )
    return issues
