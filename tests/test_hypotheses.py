"""Step 7 acceptance tests: hypothesis structure, safety, traceability, evidence gaps."""

from datetime import datetime

from sqlalchemy.orm import Session

from portfoliopulse import models as m
from portfoliopulse.hypotheses import (
    DEFINITIVE_MARKERS,
    create_draft_hypotheses,
    validate_hypothesis_draft,
)

NOW = datetime(2026, 10, 1, 12, 0, 0)


def make_supported_claim(
    session, run, company, *, text="Claim text for a supported fact.", category="acquisition"
):
    claim = m.Claim(
        id=m.new_id("claim"),
        run_id=run.id,
        company_id=company.id,
        claim_text=text,
        category=category,
        supporting_excerpt="excerpt for the fact",
        verification_status="supported",
    )
    session.add(claim)
    return claim


def make_company_run(session, *, focus="integration"):
    company = m.Company(id=m.new_id("company"), name="Northstar Components (fixture)")
    session.add(company)
    run = m.ResearchRun(
        id=m.new_id("run"), company_id=company.id, research_focus=focus, status="queued"
    )
    session.add(run)
    session.flush()
    return company, run


# --- happy structure ---------------------------------------------------------------


def test_draft_has_full_traceable_structure(session: Session):
    company, run = make_company_run(session)
    claim = make_supported_claim(
        session, run, company, text="Northstar acquired Meridian for 48 million euros in July 2026."
    )
    source = m.Source(
        id=m.new_id("source"),
        run_id=run.id,
        company_id=company.id,
        url="https://news.example.test/1",
        canonical_url="https://news.example.com/meridian-acquisition",
        publisher="Trade Press",
        status="fetched",
        content_hash=m.new_id("hash"),
        retrieved_at=NOW,
    )
    session.add(source)
    claim.sources.append(source)
    claim_2 = make_supported_claim(
        session, run, company, text="Northstar also acquired Talos Logistics in the same quarter."
    )
    session.flush()

    hypotheses, warnings = create_draft_hypotheses(session, run)
    session.commit()

    assert warnings == []
    assert len(hypotheses) == 1
    hyp = hypotheses[0]
    assert hyp.status == "draft"
    assert hyp.hypothesis_text.startswith("DRAFT —")
    assert "may be increasing coordination demands" in hyp.hypothesis_text
    # full traceability: direct fact + its source URL, kept apart from interpretation
    facts = hyp.evidence_summary["direct_facts"]
    assert facts[0]["claim_id"] == claim.id
    assert facts[1]["claim_id"] == claim_2.id
    assert facts[0]["source_urls"] == ["https://news.example.test/1"]
    interp = hyp.evidence_summary["interpretation"]
    assert interp["kind"] == "inference"
    assert "NOT a fact" in interp["note"]
    assert (
        set(hyp.alternative_explanations)
        and set(hyp.missing_evidence)
        and set(hyp.questions_to_validate)
    )
    # the linked claims round-trip through the junction
    assert [c.id for c in hyp.claims] == [claim.id, claim_2.id]


# --- unsafe / malformed drafts ------------------------------------------------------


def test_nonexistent_claim_ids_flagged(session: Session):
    issues = validate_hypothesis_draft(
        session,
        run_id="whatever",
        title="t",
        hypothesis_text="The company may face coordination demands.",
        alternative_explanations=["alt"],
        missing_evidence=["missing"],
        questions_to_validate=["q?"],
        supporting_claim_ids=["claim_ghost_1"],
    )
    assert any("nonexistent claim id" in issue and "claim_ghost_1" in issue for issue in issues)


def test_missing_alternative_is_flagged(session: Session):
    issues = validate_hypothesis_draft(
        session,
        title="t",
        hypothesis_text="The company may face coordination demands.",
        alternative_explanations=[],
        missing_evidence=["missing"],
        questions_to_validate=["q?"],
    )
    assert any("alternative explanation" in issue for issue in issues)


def test_inference_as_fact_is_flagged(session: Session):
    issues = validate_hypothesis_draft(
        session,
        title="t",
        hypothesis_text="The organisation is inefficient, which proves poor management.",
        alternative_explanations=["alt"],
        missing_evidence=["missing"],
        questions_to_validate=["q?"],
        supporting_claim_ids=[],
    )
    assert any("proven fact" in issue or "no preliminary hedge" in issue for issue in issues)
    assert any("inefficient" in issue for issue in issues)
    assert any("poor management" in issue for issue in issues)


def test_unsupported_decision_recommendation_is_flagged(session: Session):
    issues = validate_hypothesis_draft(
        session,
        title="t",
        hypothesis_text="We recommend divesting the unit immediately.",
        alternative_explanations=["alt"],
        missing_evidence=["missing"],
        questions_to_validate=["q?"],
    )
    assert any("we recommend" in issue for issue in issues)


def test_empty_evidence_set_yields_insufficient_evidence_warning(session: Session):
    _company, run = make_company_run(session)
    hypotheses, warnings = create_draft_hypotheses(session, run)
    session.commit()
    assert hypotheses == []
    assert len(warnings) == 1
    assert "insufficient supported evidence" in warnings[0]


# --- generator passes its own gate (defense in depth) --------------------------------


def test_generated_drafts_survive_the_quality_gate(session: Session):
    company, run = make_company_run(session)
    make_supported_claim(session, run, company, text="A 1", category="acquisition")
    make_supported_claim(session, run, company, text="A 2", category="acquisition")
    hypotheses, warnings = create_draft_hypotheses(session, run)
    session.commit()
    assert warnings == []
    assert len(hypotheses) == 1
    # nothing overstated slipped through the template
    hyp = hypotheses[0]
    lowered = hyp.hypothesis_text.lower()
    assert not any(marker in lowered for marker in DEFINITIVE_MARKERS)
