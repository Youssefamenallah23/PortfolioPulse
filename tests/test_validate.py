"""Step 5 acceptance tests: evidence validation, contradiction handling, honest failure."""

import json
from datetime import datetime

from portfoliopulse import models as m
from portfoliopulse.extract import MockClaimExtractor, extract_source_claims
from portfoliopulse.validate import (
    ISSUE_CLAIM_OVERSTATES,
    ISSUE_CONFLICTING,
    ISSUE_DATE_CONFUSION,
    ISSUE_EXCERPT_NOT_FOUND,
    ISSUE_MISSING_SOURCE,
    are_contradictory,
    open_issues_for_claim,
    validate_run_claims,
)

NOW = datetime(2026, 10, 1, 12, 0, 0)


def make_run(session):
    company = m.Company(id=m.new_id("company"), name="Northstar Components (fixture)")
    session.add(company)
    run = m.ResearchRun(
        id=m.new_id("run"), company_id=company.id, research_focus="fixture", status="running"
    )
    session.add(run)
    session.flush()
    return company, run


def fetched_source(
    session,
    company,
    run,
    text,
    *,
    publisher="example.test",
    url=None,
    published_at=None,
    retrieved_at=None,
):
    url = url or "https://" + publisher.replace("www.", "") + "/page-" + m.new_id("u")
    source, _ = m.ingest_source(
        session,
        run.id,
        company.id,
        url=url,
        canonical_url=url,
        content_hash="sha256:" + m.new_id("h"),
        status="fetched",
        publisher=publisher,
        published_at=published_at,
        retrieved_at=retrieved_at or NOW,
    )
    session.add(m.SourceText(source_id=source.id, text=text))
    session.flush()
    return source


def claim_via_extraction(session, run, source, text, raw_claim, marker):
    """Route claims through Step 4 so linkage is created the real way."""
    fixture = json.dumps({"claims": [raw_claim]})
    results = extract_source_claims(
        session,
        run,
        source,
        text,
        extractor=MockClaimExtractor({marker: fixture}),
        focus="fixture",
    )
    assert results.ok and results.claims
    return results.claims[0]


# --- clean claim -----------------------------------------------------------------


def test_well_supported_claim_passes(session):
    company, run = make_run(session)
    source = fetched_source(
        session, company, run, "The company acquired Meridian for approximately 48 million euros."
    )
    claim = claim_via_extraction(
        session,
        run,
        source,
        "The company acquired Meridian for approximately 48 million euros.",
        {
            "claim_text": "The company acquired Meridian for approximately 48 million euros.",
            "category": "acquisition",
            "supporting_excerpt": "acquired Meridian for approximately 48 million euros",
        },
        "acquired Meridian",
    )
    report = validate_run_claims(session, run)
    session.commit()
    assert claim.verification_status == "supported"  # evidence-backed, but NOT human-approved
    assert claim.review_status == "pending"
    totals = report.totals()
    assert totals["supported"] == 1 and totals["needs_review"] == 0 and totals["conflicting"] == 0


def test_claim_without_linked_source_flagged(session):
    company, run = make_run(session)
    claim = m.Claim(
        id=m.new_id("claim"),
        run_id=run.id,
        company_id=company.id,
        claim_text="A claim with no source recorded anywhere.",  # >10 chars for safety
        supporting_excerpt="no source given for this claim text",
        extraction_status="extracted",
    )
    session.add(claim)
    session.flush()
    report = validate_run_claims(session, run)
    session.commit()
    assert claim.verification_status != "supported"
    assert ISSUE_MISSING_SOURCE in [i.issue_type for i in report.issues]


def test_excerpt_not_in_source_text_flagged(session):
    company, run = make_run(session)
    source = fetched_source(session, company, run, "Completely unrelated content about warehouses.")
    claim = m.Claim(
        id=m.new_id("claim"),
        run_id=run.id,
        company_id=company.id,
        claim_text="The company acquired Meridian Foods this year.",
        supporting_excerpt="acquired Meridian Foods for 48 million euros",
        category="acquisition",
        extraction_status="extracted",
    )
    session.add(claim)
    claim.sources.append(source)
    session.flush()
    report = validate_run_claims(session, run)
    session.commit()
    issue_types = [i.issue_type for i in report.issues]
    assert ISSUE_EXCERPT_NOT_FOUND in issue_types
    assert claim.verification_status == "unverified"  # never "verified" doubtful evidence


# --- overreach: numbers the evidence does not contain -----------------------------


def test_claim_overstates_excerpt_with_invented_figure(session):
    company, run = make_run(session)
    source = fetched_source(
        session, company, run, "The company acquired Meridian for approximately 48 million euros."
    )
    claim = claim_via_extraction(
        session,
        run,
        source,
        "The company acquired Meridian for approximately 48 million euros.",
        {
            "claim_text": "The company acquired Meridian for 48 million euros and 12 staff.",
            "category": "acquisition",
            "supporting_excerpt": "acquired Meridian for approximately 48 million euros",
        },
        "acquired Meridian",
    )
    report = validate_run_claims(session, run)
    session.commit()
    # "12" appears nowhere in excerpt or stored text
    overstate = [i for i in report.issues if i.issue_type == ISSUE_CLAIM_OVERSTATES]
    assert overstate and "12" in overstate[0].message
    assert claim.verification_status != "supported"


def test_matching_figures_do_not_flag(session):
    company, run = make_run(session)
    text = "Revenue grew to 48 million euros according to the company."
    source = fetched_source(session, company, run, text)
    claim = claim_via_extraction(
        session,
        run,
        source,
        text,
        {
            "claim_text": "Revenue was 48 million euros, according to the company.",
            "category": "industry_context",
            "supporting_excerpt": "Revenue grew to 48 million euros",
        },
        "Revenue grew",
    )
    report = validate_run_claims(session, run)
    session.commit()
    # "48" matches the wording exactly
    assert not [i for i in report.issues if i.issue_type == ISSUE_CLAIM_OVERSTATES]
    assert claim.verification_status == "supported"


# --- date confusion ---------------------------------------------------------------


def test_event_date_after_publication_without_future_intent_flagged(session):
    company, run = make_run(session)
    text = "The acquisition was completed on 2027-03-01."
    source = fetched_source(
        session,
        company,
        run,
        text,
        published_at=NOW,  # published 2026-10-01
    )
    claim = claim_via_extraction(
        session,
        run,
        source,
        text,
        {
            "claim_text": "The acquisition was completed on 2027-03-01.",
            "category": "acquisition",
            "supporting_excerpt": "completed on 2027-03-01",
            "event_date": "2027-03-01",
        },
        "completed on 2027",
    )
    report = validate_run_claims(session, run)
    session.commit()
    assert [i.issue_type for i in report.issues].count(ISSUE_DATE_CONFUSION) == 1
    assert claim.verification_status != "supported"


def test_future_plans_with_later_date_are_fine(session):
    company, run = make_run(session)
    text = "The company plans to open the new hub by 2027-06-01."
    source = fetched_source(session, company, run, text, published_at=NOW)
    claim = claim_via_extraction(
        session,
        run,
        source,
        text,
        {
            "claim_text": "The company plans to open the new hub by 2027-06-01.",
            "category": "strategy",
            "supporting_excerpt": "plans to open the new hub by 2027-06-01",
            "event_date": "2027-06-01",  # 'by 2027' + 'plans to' → future intent
        },
        "plans to open",
    )
    report = validate_run_claims(session, run)
    session.commit()
    assert not [i for i in report.issues if i.issue_type == ISSUE_DATE_CONFUSION]
    assert claim.verification_status == "supported"


# --- conflicting evidence: recorded, never resolved -------------------------------


def test_conflicting_figures_across_sources_flagged(session):
    company, run = make_run(session)
    text_a = "The acquisition was worth 48 million euros."
    text_b = "The same acquisition was worth 60 million euros."
    src_a = fetched_source(session, company, run, text_a, publisher="company.example.test")
    src_b = fetched_source(session, company, run, text_b, publisher="press.example.test")

    raw = lambda value: {
        "claim_text": f"The acquisition was worth {value} million euros.",
        "category": "acquisition",
        "supporting_excerpt": f"acquisition was worth {value} million euros",
    }
    claim_a = claim_via_extraction(
        session, run, src_a, text_a, raw("48"), "acquisition was worth 48"
    )
    claim_b = claim_via_extraction(
        session, run, src_b, text_b, raw("60"), "acquisition was worth 60"
    )

    report = validate_run_claims(session, run)
    session.commit()
    conflicts = [i for i in report.issues if i.issue_type == ISSUE_CONFLICTING]
    assert len(conflicts) == 1
    assert {conflicts[0].claim_id, conflicts[0].related_claim_id} == {claim_a.id, claim_b.id}
    assert "analyst decides" in conflicts[0].message
    # BOTH sides are conflicting — the system does not pick a winner, ever
    assert claim_a.verification_status == claim_b.verification_status == "conflicting"
    assert report.totals()["conflicting"] == 2
    # both sides' issues stay open and queryable for the review UI
    assert open_issues_for_claim(session, claim_a.id)
    assert open_issues_for_claim(session, claim_b.id)


def test_conflicting_event_dates_across_sources_flagged(session):
    company, run = make_run(session)
    text_a = "The deal closing was signed on 2026-06-01."
    text_b = "The deal closing was signed on 2026-09-01."
    src_a = fetched_source(
        session, company, run, text_a, publisher="a.example.test", published_at=NOW
    )
    src_b = fetched_source(session, company, run, text_b, publisher="b.example.test")

    raw = lambda value: {
        "claim_text": f"The deal closing was signed on {value}.",
        "category": "acquisition",
        "supporting_excerpt": f"deal closing was signed on {value}",
        "event_date": value,
    }
    claim_a = claim_via_extraction(
        session, run, src_a, text_a, raw("2026-06-01"), "deal closing was signed on 2026-06"
    )
    claim_b = claim_via_extraction(
        session, run, src_b, text_b, raw("2026-09-01"), "deal closing was signed on 2026-09"
    )

    report = validate_run_claims(session, run)
    assert [i.issue_type for i in report.issues].count(ISSUE_CONFLICTING) == 1
    assert claim_a.verification_status == claim_b.verification_status == "conflicting"


def test_unrelated_claims_never_auto_conflict(session):
    company, run = make_run(session)
    text_a = (
        "The executive outlined a regional replication programme with distinctive vocabulary alpha."
    )
    text_b = "The treasurer reported quarterly customer retention metrics using vocabulary omega."
    src_a = fetched_source(session, company, run, text_a, publisher="a.example.test")
    src_b = fetched_source(session, company, run, text_b, publisher="b.example.test")
    raw_a = {
        "claim_text": "The executive outlined a regional replication programme with distinctive vocabulary alpha.",
        "category": "industry_context",  # same category on purpose
        "supporting_excerpt": "outlined a regional replication programme with distinctive vocabulary",
    }
    raw_b = {
        "claim_text": "The treasurer reported quarterly customer retention metrics using vocabulary omega.",
        "category": "industry_context",  # same category on purpose
        "supporting_excerpt": "reported quarterly customer retention metrics using vocabulary",
    }
    claim_a = claim_via_extraction(
        session, run, src_a, text_a, raw_a, "regional replication programme"
    )
    claim_via_extraction(session, run, src_b, text_b, raw_b, "customer retention metrics")

    report = validate_run_claims(session, run)
    assert [i for i in report.issues if i.issue_type == ISSUE_CONFLICTING] == []
    # low lexical overlap overrides same-category suspicion
    assert claim_a.verification_status == "supported"


# --- unit-level classifier sanity --------------------------------------------------


def test_are_contradictory_requires_same_category_and_overlap():
    text = "claim instance fourteen words about growth"
    a = m.Claim(
        id="c1",
        run_id="r",
        company_id="co",
        claim_text=text + " 48 million",
        supporting_excerpt="x",
        category="acquisition",
    )
    b = m.Claim(
        id="c2",
        run_id="r",
        company_id="co",
        claim_text=text + " 60 million",
        supporting_excerpt="x",
        category="acquisition",
    )
    assert are_contradictory(a, a) is None
    assert are_contradictory(a, b) == "figures"
