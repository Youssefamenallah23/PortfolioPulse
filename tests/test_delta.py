"""Delta mode: pulse events vs the last approved run of the same company."""

from datetime import UTC, datetime, timedelta

from portfoliopulse import models as m
from portfoliopulse.delta import delta_for_run, last_approved_run


def make_company_and_run(session, *, approved: bool, minutes_ago: int) -> m.ResearchRun:
    company = m.Company(id=m.new_id("company"), name="Northstar Components (fixture)")
    session.add(company)
    run = m.ResearchRun(
        id=m.new_id("run"),
        company_id=company.id,
        research_focus="fixture focus",
        status="approved" if approved else "awaiting_review",
        finished_at=datetime.now(UTC).replace(tzinfo=None)
        - timedelta(minutes=minutes_ago),
    )
    session.add(run)
    session.flush()
    return run


def make_sibling_run(
    session, company: m.Company, *, approved: bool, minutes_ago: int
) -> m.ResearchRun:
    run = m.ResearchRun(
        id=m.new_id("run"),
        company_id=company.id,
        research_focus="fixture focus",
        status="approved" if approved else "awaiting_review",
        finished_at=datetime.now(UTC).replace(tzinfo=None)
        - timedelta(minutes=minutes_ago),
    )
    session.add(run)
    session.flush()
    return run


def add_claim(
    session,
    run: m.ResearchRun,
    text: str,
    *,
    category: str = "acquisition",
    review: str = "approved",
) -> m.Claim:
    claim = m.Claim(
        id="claim_" + m.new_id("claim")[6:],
        run_id=run.id,
        company_id=run.company_id,
        claim_text=text,
        category=category,
        supporting_excerpt=text,
        verification_status="supported",
        review_status=review,
    )
    session.add(claim)
    session.flush()
    return claim


def test_first_approved_run_is_the_baseline(session):
    run = make_company_and_run(session, approved=True, minutes_ago=10)
    add_claim(session, run, "Northstar acquired Meridian for 48 million euros.")
    assert delta_for_run(session, run) is None


def test_unchanged_new_and_contradicting_claims(session):
    baseline = make_company_and_run(session, approved=True, minutes_ago=120)
    prior = make_sibling_run(session, baseline.company, approved=True, minutes_ago=60)
    add_claim(session, prior, "Northstar acquired Meridian for 48 million euros.")
    add_claim(
        session,
        prior,
        "Northstar rejected the trade-press figure of 60 million euros.",
        review="rejected",  # rejected claims are not part of the accepted record
    )

    current = make_sibling_run(session, baseline.company, approved=True, minutes_ago=5)
    add_claim(session, current, "Northstar acquired Meridian for 48 million euros.")  # unchanged
    add_claim(
        session,
        current,
        "Northstar acquired Meridian for 60 million euros.",  # clashes with 48M record
    )
    add_claim(session, current, "Northstar opened an office in Oslo.")  # new
    add_claim(
        session,
        current,
        "Northstar reviewed its pricing policy.",
        review="pending",
        category="strategy",
    )

    delta = delta_for_run(session, current)
    assert delta is not None
    assert delta["prior_run_id"] == prior.id
    assert delta["unchanged"] == ["Northstar acquired Meridian for 48 million euros."]
    # pending claims are still findings; only rejected material stays out of the record
    assert delta["new"] == [
        "Northstar opened an office in Oslo.",
        "Northstar reviewed its pricing policy.",
    ]
    assert len(delta["contradicts"]) == 1
    clash = delta["contradicts"][0]
    assert "60 million" in clash["claim"]
    assert "48 million" in clash["prior"]


def test_latest_approved_run_wins_and_rejected_prior_claims_do_not_conflict(session):
    baseline = make_company_and_run(session, approved=True, minutes_ago=120)
    add_claim(session, baseline, "Northstar had 200 staff.")

    prior = make_sibling_run(session, baseline.company, approved=True, minutes_ago=30)
    add_claim(session, prior, "Northstar had 240 staff.")

    current = make_sibling_run(session, baseline.company, approved=True, minutes_ago=10)
    add_claim(session, current, "Northstar had 300 staff.")

    assert last_approved_run(session, current).id == prior.id
    delta = delta_for_run(session, current)
    # the clash is judged against the LATEST approved record (240), not the oldest (200)
    assert delta["contradicts"][0]["prior"] == "Northstar had 240 staff."
