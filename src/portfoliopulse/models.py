"""Evidence-chain data model: Company → ResearchRun → Source → Claim → Hypothesis → Review.

Conventions
- IDs are prefixed strings (`comp_…`, `run_…`, `src_…`, `claim_…`, `hyp_…`, `rev_…`).
- All timestamps are naive UTC (see db.utcnow).
- Statuses (documented vocabulary used across the pipeline):
  Source.status: fetched | failed | blocked | duplicate | unsupported_content_type
  Claim.category: company_profile | ownership | acquisition | strategy | leadership |
                  hiring_signal | geographic_expansion | industry_context
  Claim.verification_status: unverified | supported | unsupported | conflicting
  Claim.review_status: pending | approved | edited | rejected
  Hypothesis.status / run status: draft | approved | edited | rejected (draft until a
  persisted Review exists — nothing becomes "approved" without a Review row).
- A claim with a category in EVENT_CATEGORIES and a non-null event_date is treated as
  the system's "Event" record; the exports step derives the Events sheet from these.
  A separate Event table was deliberately not created (single source of truth).
"""

import uuid
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship

EVENT_CATEGORIES = {"acquisition", "leadership", "geographic_expansion", "strategy"}


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def utcnow() -> datetime:
    """Naive UTC 'now' — the convention used in every persisted timestamp."""
    return datetime.now(UTC).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class Company(Base):
    __tablename__ = "companies"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    website: Mapped[str | None] = mapped_column(String(500))
    sector: Mapped[str | None] = mapped_column(String(200))
    country: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class ResearchRun(Base):
    __tablename__ = "research_runs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    company_id: Mapped[str] = mapped_column(ForeignKey("companies.id"), index=True)
    research_focus: Mapped[str] = mapped_column(String(500))
    # queued | running | awaiting_review | approved | failed
    status: Mapped[str] = mapped_column(String(30), default="queued")
    started_at: Mapped[datetime | None] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    error_summary: Mapped[str | None] = mapped_column(Text)
    # Configuration summary WITHOUT secrets (provider names + limits only).
    config_summary: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    # Denormalised counts, refreshed at finalize/run-report time.
    source_count: Mapped[int] = mapped_column(Integer, default=0)
    claim_count: Mapped[int] = mapped_column(Integer, default=0)
    hypothesis_count: Mapped[int] = mapped_column(Integer, default=0)

    company: Mapped[Company] = relationship(lazy="joined")


class Source(Base):
    __tablename__ = "sources"
    __table_args__ = (
        UniqueConstraint("run_id", "canonical_url", name="uq_source_run_canonical_url"),
        UniqueConstraint("run_id", "content_hash", name="uq_source_run_content_hash"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("research_runs.id"), index=True)
    company_id: Mapped[str] = mapped_column(ForeignKey("companies.id"), index=True)
    url: Mapped[str] = mapped_column(String(1000))
    canonical_url: Mapped[str] = mapped_column(String(1000))
    title: Mapped[str | None] = mapped_column(String(500))
    publisher: Mapped[str | None] = mapped_column(String(300))
    source_type: Mapped[str | None] = mapped_column(String(100))
    published_at: Mapped[datetime | None] = mapped_column(DateTime)  # unknown stays null
    retrieved_at: Mapped[datetime | None] = mapped_column(DateTime)
    http_status: Mapped[int | None] = mapped_column(Integer)
    content_hash: Mapped[str | None] = mapped_column(String(80))
    # fetched | failed | blocked | duplicate | unsupported_content_type
    status: Mapped[str] = mapped_column(String(30), default="fetched")
    # pending | extracted | failed_extraction — null when untouched by extraction yet
    extraction_status: Mapped[str | None] = mapped_column(String(30))
    excerpt: Mapped[str | None] = mapped_column(Text)

    # ORM edges give the unit-of-work FK insert ordering (source after run/company) —
    # without a relationship, bare-FK rows get no dependency guarantee between mappers.
    run: Mapped[ResearchRun] = relationship(lazy="joined")
    company: Mapped[Company] = relationship(lazy="joined")


class SourceText(Base):
    """Extracted page text, kept off the hot Source row to avoid repeating large bodies."""

    __tablename__ = "source_text"

    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id"), primary_key=True)
    text: Mapped[str] = mapped_column(Text)


# A claim may be supported by several sources (corroboration); use the junction.
class ClaimSource(Base):
    __tablename__ = "claim_sources"

    claim_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("claims.id"), index=True, primary_key=True
    )
    source_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("sources.id"), index=True, primary_key=True
    )


class Claim(Base):
    __tablename__ = "claims"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("research_runs.id"), index=True)
    company_id: Mapped[str] = mapped_column(ForeignKey("companies.id"), index=True)
    claim_text: Mapped[str] = mapped_column(Text)
    category: Mapped[str | None] = mapped_column(String(50))
    supporting_excerpt: Mapped[str] = mapped_column(Text)
    event_date: Mapped[date | None] = mapped_column(
        Date
    )  # date the claim concerns; null if unknown
    # extracted | failed_extraction
    extraction_status: Mapped[str] = mapped_column(String(30), default="extracted")
    # unverified | supported | unsupported | conflicting
    verification_status: Mapped[str] = mapped_column(String(30), default="unverified")
    # pending | approved | edited | rejected
    review_status: Mapped[str] = mapped_column(String(30), default="pending")
    llm_confidence: Mapped[float | None] = mapped_column(Float)  # model's self-report only
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    sources: Mapped[list[Source]] = relationship(secondary="claim_sources", lazy="selectin")
    run: Mapped[ResearchRun] = relationship(lazy="joined")
    company: Mapped[Company] = relationship(lazy="joined")


class HypothesisClaim(Base):
    __tablename__ = "hypothesis_claims"

    hypothesis_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("hypotheses.id"), index=True, primary_key=True
    )
    claim_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("claims.id"), index=True, primary_key=True
    )


class Hypothesis(Base):
    __tablename__ = "hypotheses"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("research_runs.id"), index=True)
    company_id: Mapped[str] = mapped_column(ForeignKey("companies.id"), index=True)
    title: Mapped[str] = mapped_column(String(300))
    hypothesis_text: Mapped[str] = mapped_column(Text)
    alternative_explanations: Mapped[list[str]] = mapped_column(JSON, default=list)
    missing_evidence: Mapped[list[str]] = mapped_column(JSON, default=list)
    questions_to_validate: Mapped[list[str]] = mapped_column(JSON, default=list)
    # Structured Step 7 evidence summary:
    # {"direct_facts": [{"claim_id", "claim_text", "source_urls"}],
    #  "interpretation": {"text", "kind": "inference", "note"}}
    evidence_summary: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    # draft | approved | edited | rejected
    status: Mapped[str] = mapped_column(String(30), default="draft")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    claims: Mapped[list[Claim]] = relationship(secondary="hypothesis_claims", lazy="selectin")
    run: Mapped[ResearchRun] = relationship(lazy="joined")
    company: Mapped[Company] = relationship(lazy="joined")


class Review(Base):
    """Persisted, auditable human decision on any claim or hypothesis."""

    __tablename__ = "reviews"
    __table_args__ = (Index("ix_reviews_target", "target_type", "target_id"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    target_type: Mapped[str] = mapped_column(String(20))  # claim | hypothesis
    target_id: Mapped[str] = mapped_column(String(40))
    decision: Mapped[str] = mapped_column(String(20))  # approved | edited | rejected | flagged
    reviewer_notes: Mapped[str | None] = mapped_column(Text)
    edited_value: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ValidationIssue(Base):
    """Deterministic/automated validation outcome on a claim, persisted and auditable.

    issue_type vocabulary (stable, documented):
      missing_source | missing_source_metadata | source_text_missing |
      excerpt_not_found | claim_overstates_excerpt | date_confusion | conflicting_claims

    Structural issues attach to one claim (claim_id). Conflicts attach to a pair:
    the system records BOTH claim ids and never decides which source is correct.
    review_status follows the human review workflow (pending until resolved).
    """

    __tablename__ = "validation_issues"
    __table_args__ = (Index("ix_validation_run_type", "run_id", "issue_type"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("research_runs.id"), index=True)
    issue_type: Mapped[str] = mapped_column(String(40))
    message: Mapped[str | None] = mapped_column(Text)
    claim_id: Mapped[str | None] = mapped_column(String(40), ForeignKey("claims.id"), index=True)
    related_claim_id: Mapped[str | None] = mapped_column(String(40))
    # pending | resolved | waived
    review_status: Mapped[str] = mapped_column(String(20), default="pending")
    # FK insert-ordering edge (issue after its claim); related_claim_id stays a bare FK
    claim: Mapped[Claim | None] = relationship(foreign_keys="ValidationIssue.claim_id")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


# ---------------------------------------------------------------------------
# Rule-enforcing helpers — the two guarantees the pipeline relies on.
# ---------------------------------------------------------------------------


def ingest_source(
    session: Session,
    run_id: str,
    company_id: str,
    *,
    url: str,
    canonical_url: str,
    content_hash: str | None = None,
    source_id: str | None = None,
    **fields: Any,
) -> tuple[Source, bool]:
    """Idempotent source ingestion within one run.

    Returns (source, created). A second ingest of the same canonical URL or the
    same content hash returns the existing row with `created=False` — re-running
    a source must never duplicate it.
    """
    existing = (
        session.query(Source)
        .filter(
            Source.run_id == run_id,
            (Source.canonical_url == canonical_url)
            if content_hash is None
            else ((Source.canonical_url == canonical_url) | (Source.content_hash == content_hash)),
        )
        .first()
    )
    if existing:
        return existing, False
    source = Source(
        id=source_id or new_id("src"),
        run_id=run_id,
        company_id=company_id,
        url=url,
        canonical_url=canonical_url,
        content_hash=content_hash,
        retrieved_at=fields.pop("retrieved_at", utcnow()),
        **fields,
    )
    session.add(source)
    session.flush()
    return source, True


def link_claim_source(session: Session, claim: Claim, source: Source) -> None:
    exists = session.query(ClaimSource).filter_by(claim_id=claim.id, source_id=source.id).first()
    if exists is None:
        # Route through the ORM relationship so insert ordering is handled for us.
        claim.sources.append(source)
        session.flush()


def set_claim_verified(session: Session, claim: Claim) -> None:
    """A claim can never be marked supported without at least one linked source.

    Checks the junction table directly — a freshly linked claim's cached
    relationship may not reflect the new link within the same session.
    """
    linked = session.query(ClaimSource).filter_by(claim_id=claim.id).count()
    if not linked:
        raise ValueError(f"claim {claim.id} has no linked source; refusing to mark supported")
    claim.verification_status = "supported"


def record_review(
    session: Session,
    *,
    target_type: str,
    target_id: str,
    decision: str,
    reviewer_notes: str | None = None,
    edited_value: dict[str, Any] | None = None,
) -> Review:
    """Persist a review decision and, for edit/reject, sync the target's status."""
    review = Review(
        id=new_id("rev"),
        target_type=target_type,
        target_id=target_id,
        decision=decision,
        reviewer_notes=reviewer_notes,
        edited_value=edited_value,
    )
    session.add(review)
    if target_type == "claim":
        claim = session.get(Claim, target_id)
        if claim:
            if decision == "edited" and edited_value and "claim_text" in edited_value:
                claim.claim_text = edited_value["claim_text"]
            claim.review_status = {
                "approved": "approved",
                "edited": "edited",
                "rejected": "rejected",
            }.get(decision, claim.review_status)
    elif target_type == "hypothesis":
        hyp = session.get(Hypothesis, target_id)
        if hyp:
            if decision == "edited" and edited_value and "hypothesis_text" in edited_value:
                hyp.hypothesis_text = edited_value["hypothesis_text"]
            hyp.status = {"approved": "approved", "edited": "edited", "rejected": "rejected"}.get(
                decision, hyp.status
            )
    session.flush()
    return review
