"""Step 2 acceptance tests: evidence-chain persistence, integrity, dedupe, guards."""

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy.exc import IntegrityError

from portfoliopulse import models as m
from portfoliopulse.db import make_session_factory
from portfoliopulse.seed import seed_demo


def make_company(session, cid=None):
    company = m.Company(
        id=cid or m.new_id("company"),
        name="Northstar Components (fixture)",
        website="https://www.northstar-components.test",
    )
    session.add(company)
    session.flush()
    return company


def make_run(session, company, rid=None):
    run = m.ResearchRun(
        id=rid or m.new_id("run"),
        company_id=company.id,
        research_focus="acquisition integration (fixture)",
        status="running",
    )
    session.add(run)
    session.flush()
    return run


# --- round-trip across a session restart -------------------------------------


def test_full_chain_roundtrip_across_session_restart(engine, session):
    company = make_company(session)
    run = make_run(session, company)
    source, _ = m.ingest_source(
        session,
        run.id,
        company.id,
        url="https://example.test/news",
        canonical_url="https://example.test/news",
        content_hash="sha256:abc",
        title="News (fixture)",
        status="fetched",
        excerpt="Excerpt (fixture).",
    )
    claim = m.Claim(
        id="claim_" + uuid.uuid4().hex[:12],
        run_id=run.id,
        company_id=company.id,
        claim_text="The company acquired a distributor.",
        category="acquisition",
        supporting_excerpt="Excerpt (fixture).",
    )
    session.add(claim)
    m.link_claim_source(session, claim, source)
    hyp = m.Hypothesis(
        id="hyp_" + uuid.uuid4().hex[:12],
        run_id=run.id,
        company_id=company.id,
        title="t",
        hypothesis_text="h",
    )
    session.add(hyp)
    session.add(m.HypothesisClaim(hypothesis_id=hyp.id, claim_id=claim.id))
    session.commit()

    # "Restart": brand-new session on the same engine.
    session2 = make_session_factory(engine)()
    loaded_claim = session2.get(m.Claim, claim.id)
    assert [s.id for s in loaded_claim.sources] == [source.id]
    loaded_hyp = session2.get(m.Hypothesis, hyp.id)
    assert [c.id for c in loaded_hyp.claims] == [claim.id]
    assert loaded_claim.run.company.name == company.name
    session2.close()


def test_naive_utc_timestamps(session):
    before = datetime.now(UTC).replace(tzinfo=None)
    company = make_company(session)
    after = datetime.now(UTC).replace(tzinfo=None)
    assert before <= company.created_at <= after
    assert company.created_at.tzinfo is None  # stored naive-UTC by convention


# --- referential integrity ----------------------------------------------------


def test_claim_with_missing_run_is_rejected(session):
    company = make_company(session)
    claim = m.Claim(
        id="claim_" + uuid.uuid4().hex[:12],
        run_id="run_does_not_exist",
        company_id=company.id,
        claim_text="x",
        supporting_excerpt="y",
    )
    session.add(claim)
    with pytest.raises(IntegrityError):
        session.commit()


def test_hypothesis_claim_with_missing_claim_is_rejected(session):
    company = make_company(session)
    run = make_run(session, company)
    hyp = m.Hypothesis(
        id="hyp_" + uuid.uuid4().hex[:12],
        run_id=run.id,
        company_id=company.id,
        title="t",
        hypothesis_text="h",
    )
    session.add(hyp)
    session.flush()
    session.add(m.HypothesisClaim(hypothesis_id=hyp.id, claim_id="claim_missing"))
    with pytest.raises(IntegrityError):
        session.commit()


# --- verification guard --------------------------------------------------------


def test_claim_cannot_be_verified_without_linked_source(session):
    company = make_company(session)
    run = make_run(session, company)
    claim = m.Claim(
        id="claim_" + uuid.uuid4().hex[:12],
        run_id=run.id,
        company_id=company.id,
        claim_text="x",
        supporting_excerpt="y",
    )
    session.add(claim)
    session.flush()
    with pytest.raises(ValueError, match="no linked source"):
        m.set_claim_verified(session, claim)
    assert claim.verification_status == "unverified"  # unchanged


def test_claim_with_source_can_be_verified(session):
    company = make_company(session)
    run = make_run(session, company)
    source, _ = m.ingest_source(
        session,
        run.id,
        company.id,
        url="https://example.test/a",
        canonical_url="https://example.test/a",
        content_hash="sha256:1",
        status="fetched",
    )
    claim = m.Claim(
        id="claim_" + uuid.uuid4().hex[:12],
        run_id=run.id,
        company_id=company.id,
        claim_text="x",
        supporting_excerpt="y",
    )
    session.add(claim)
    session.flush()
    m.link_claim_source(session, claim, source)
    m.set_claim_verified(session, claim)
    assert claim.verification_status == "supported"


# --- idempotent source ingestion ------------------------------------------------


def test_source_ingest_deduplicates_on_url_and_hash(session):
    company = make_company(session)
    run = make_run(session, company)
    kwargs = {
        "url": "https://example.test/news?utm_source=x",
        "canonical_url": "https://example.test/news",
        "content_hash": "sha256:dedupe-me",
        "title": "News (fixture)",
        "status": "fetched",
    }
    first, created1 = m.ingest_source(session, run.id, company.id, **kwargs)
    same_url, created2 = m.ingest_source(
        session,
        run.id,
        company.id,
        url="https://example.test/news?utm_source=other",  # same canonical URL, referrer-stripped
        canonical_url="https://example.test/news",
        status="fetched",
    )
    assert created1 and not created2
    assert first.id == same_url.id

    same_hash, created3 = m.ingest_source(
        session,
        run.id,
        company.id,
        url="https://mirror.test/news",
        canonical_url="https://mirror.test/news",
        content_hash="sha256:dedupe-me",
        status="fetched",
    )
    assert not created3
    assert first.id == same_hash.id
    assert session.query(m.Source).filter_by(run_id=run.id).count() == 1


def test_unique_constraints_block_raw_duplicates(session):
    company = make_company(session)
    run = make_run(session, company)
    _source, _ = m.ingest_source(
        session,
        run.id,
        company.id,
        url="https://example.test/a",
        canonical_url="https://example.test/a",
        content_hash="sha256:h",
    )
    session.add(
        m.Source(
            id="src_" + uuid.uuid4().hex[:12],
            run_id=run.id,
            company_id=company.id,
            url="https://example.test/a",
            canonical_url="https://example.test/a",
            content_hash="sha256:other",
        )
    )
    with pytest.raises(IntegrityError):
        session.commit()


# --- reviews ----------------------------------------------------------------------


def test_review_persists_and_updates_target(session):
    ids = seed_demo(session)
    review = m.record_review(
        session,
        target_type="hypothesis",
        target_id=ids["hypothesis_id"],
        decision="edited",
        reviewer_notes="Keep this as an investigation question, not a finding.",
        edited_value={"hypothesis_text": "Edited (fixture)."},
    )
    hyp = session.get(m.Hypothesis, ids["hypothesis_id"])
    assert hyp.status == "edited"
    assert hyp.hypothesis_text == "Edited (fixture)."
    assert review.created_at is not None

    rejected = m.record_review(
        session, target_type="claim", target_id=ids["claim_id"], decision="rejected"
    )
    assert session.get(m.Claim, ids["claim_id"]).review_status == "rejected"
    assert session.query(m.Review).count() == 2
    assert rejected.id.startswith("rev_")


def test_review_of_unknown_target_is_recorded_but_target_untouched(session):
    review = m.record_review(
        session, target_type="claim", target_id="claim_ghost", decision="approved"
    )
    assert review.target_id == "claim_ghost"
    assert session.query(m.Review).count() == 1  # audit trail keeps the attempt


# --- seed fixture -----------------------------------------------------------------


def test_seed_demo_exact_shape(session):
    ids = seed_demo(session)
    company = session.get(m.Company, ids["company_id"])
    run = session.get(m.ResearchRun, ids["run_id"])
    assert "fixture" in company.name
    assert company.website.endswith(".test")
    assert run.status == "awaiting_review"
    assert run.source_count == 2
    claim = session.get(m.Claim, ids["claim_id"])
    assert claim.verification_status == "supported" and len(claim.sources) == 1
    assert claim.event_date is None  # never invented
    hyp = session.get(m.Hypothesis, ids["hypothesis_id"])
    assert hyp.status == "draft"
    assert [c.id for c in hyp.claims] == [ids["claim_id"]]
    # config summary never contains credentials
    assert "key" not in str(run.config_summary).lower()


def test_seed_and_review_block_approval_boundary(session):
    """The seeded run must not count as approved without a persisted review."""
    ids = seed_demo(session)
    run = session.get(m.ResearchRun, ids["run_id"])
    assert run.status != "approved"
    assert session.query(m.Review).count() == 0
