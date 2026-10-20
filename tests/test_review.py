"""Step 8 acceptance tests: review persistence, audit trail, API, export gate."""

from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from portfoliopulse import models as m
from portfoliopulse.pipeline import ApprovalBlocked, approve_run
from portfoliopulse.review import (
    ReviewInputError,
    ReviewNotFoundError,
    resolve_issue,
    review_claim,
    review_hypothesis,
    unresolved_summary,
    visible_claims,
)

NOW = datetime(2026, 10, 1, 12, 0, 0)


@pytest.fixture
def data(session):
    """A run mid-review: 2 claims (one with sources), 1 draft hypothesis, 1 open issue."""
    company = m.Company(id=m.new_id("company"), name="Northstar Components (fixture)")
    session.add(company)
    run = m.ResearchRun(
        id=m.new_id("run"),
        company_id=company.id,
        research_focus="integration",
        status="awaiting_review",
    )
    session.add(run)
    source = m.Source(
        id=m.new_id("source"),
        run_id=run.id,
        company_id=company.id,
        url="https://news.example.test/1",
        canonical_url="https://news.example.com/1",
        publisher="Trade Press",
        status="fetched",
        content_hash=m.new_id("hash"),
        retrieved_at=NOW,
    )
    session.add(source)
    claim_1 = m.Claim(
        id=m.new_id("claim"),
        run_id=run.id,
        company_id=company.id,
        claim_text="Northstar acquired Meridian for 48 million euros.",
        category="acquisition",
        supporting_excerpt="acquired Meridian for 48 million euros",
        verification_status="supported",
        review_status="pending",
    )
    session.add(claim_1)
    claim_1.sources.append(source)
    claim_2 = m.Claim(
        id=m.new_id("claim"),
        run_id=run.id,
        company_id=company.id,
        claim_text="Northstar entered two new regions this year.",
        category="geographic_expansion",
        supporting_excerpt="entered two new regions",
        verification_status="conflicting",
        review_status="pending",
    )
    session.add(claim_2)
    session.flush()  # bare FK columns (issues) get no dependency edge; flush claims first
    hypothesis = m.Hypothesis(
        id=m.new_id("hyp"),
        run_id=run.id,
        company_id=company.id,
        title="Potential integration complexity",
        hypothesis_text="DRAFT — the acquisitions may increase coordination demands.",
        alternative_explanations=["An integration function may already exist."],
        missing_evidence=["Operating model and decision rights"],
        questions_to_validate=["Who owns integration decisions?"],
        evidence_summary={"direct_facts": [], "interpretation": {}},
        status="draft",
    )
    session.add(hypothesis)
    hypothesis.claims.append(claim_1)
    issue = m.ValidationIssue(
        id=m.new_id("issue"),
        run_id=run.id,
        issue_type="conflicting_claims",
        message="48 vs 60 million euros reported",
        claim_id=claim_2.id,
        review_status="pending",
    )
    session.add(issue)
    session.commit()
    return SimpleRecord(company, run, claim_1, claim_2, hypothesis, issue)


class SimpleRecord:
    def __init__(self, company, run, claim_1, claim_2, hypothesis, issue):
        self.company = company
        self.run = run
        self.claim_1 = claim_1
        self.claim_2 = claim_2
        self.hypothesis = hypothesis
        self.issue = issue


@pytest.fixture
def client(session, data):
    from portfoliopulse.main import app, get_db

    app.dependency_overrides[get_db] = lambda: session
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


# --- service: claim review ----------------------------------------------------------


def test_approve_claim_persists_audit_trail(session, data):
    review = review_claim(
        session, claim_id=data.claim_1.id, decision="approved", reviewer_notes="verified figure"
    )
    session.commit()
    row = session.get(m.Review, review.id)
    assert row.target_type == "claim"
    assert row.target_id == data.claim_1.id
    assert row.decision == "approved"
    assert row.reviewer_notes == "verified figure"
    assert row.created_at is not None
    assert data.claim_1.review_status == "approved"


def test_edit_claim_updates_wording_for_subsequent_reads(session, data):
    review_claim(
        session,
        claim_id=data.claim_1.id,
        decision="edited",
        edited_text="Northstar acquired Meridian for approximately 48 million euros.",
        reviewer_notes="keep the hedge",
    )
    session.commit()
    reloaded = session.query(m.Claim).filter_by(id=data.claim_1.id).one()
    assert reloaded.claim_text == "Northstar acquired Meridian for approximately 48 million euros."
    assert reloaded.review_status == "edited"
    row = session.query(m.Review).filter_by(target_id=data.claim_1.id).one()
    assert row.edited_value == {"claim_text": reloaded.claim_text}


def test_edit_without_text_is_rejected(session, data):
    with pytest.raises(ReviewInputError, match="non-empty edited_text"):
        review_claim(session, claim_id=data.claim_1.id, decision="edited")


def test_edited_text_only_valid_for_edit(session, data):
    with pytest.raises(ReviewInputError, match="only valid"):
        review_claim(session, claim_id=data.claim_1.id, decision="approved", edited_text="x")


def test_rejected_claim_hidden_from_conclusions_but_in_audit_trail(session, data):
    review_claim(session, claim_id=data.claim_2.id, decision="rejected", reviewer_notes="unfounded")
    session.commit()
    assert data.claim_2.review_status == "rejected"
    visible = visible_claims(session, data.run)
    assert data.claim_2.id not in [claim.id for claim in visible]
    assert data.claim_1.id in [claim.id for claim in visible]
    # audit trail retains the rejected claim alongside its rejection
    log = session.query(m.Review).filter_by(target_id=data.claim_2.id, decision="rejected").count()
    assert log == 1


def test_flag_records_disagreement_but_stays_pending(session, data):
    review_claim(
        session, claim_id=data.claim_1.id, decision="flagged", reviewer_notes="conflict unresolved"
    )
    session.commit()
    assert data.claim_1.review_status == "pending"
    reviews = session.query(m.Review).filter_by(target_id=data.claim_1.id).all()
    assert [review.decision for review in reviews] == ["flagged"]
    assert unresolved_summary(session, data.run)["pending_claims"] == 2


def test_unknown_claim_and_bad_vocabulary_raise(session, data):
    with pytest.raises(ReviewNotFoundError, match="does not exist"):
        review_claim(session, claim_id="claim_ghost", decision="approved")
    with pytest.raises(ReviewInputError, match="one of"):
        review_claim(session, claim_id=data.claim_1.id, decision="publish")


# --- service: hypothesis review -----------------------------------------------------


def test_hypothesis_approve_edit_reject(session, data):
    review_hypothesis(
        session,
        hypothesis_id=data.hypothesis.id,
        decision="edited",
        edited_text="DRAFT — keep as an investigation question, not a finding.",
        reviewer_notes="investigation question",
    )
    session.commit()
    assert data.hypothesis.status == "edited"
    assert "keep as an investigation question" in data.hypothesis.hypothesis_text
    review_hypothesis(session, hypothesis_id=data.hypothesis.id, decision="approved")
    assert data.hypothesis.status == "approved"
    assert len(session.query(m.Review).filter_by(target_id=data.hypothesis.id).all()) == 2


def test_unknown_hypothesis_raises(session, data):
    with pytest.raises(ReviewNotFoundError, match="does not exist"):
        review_hypothesis(session, hypothesis_id="hyp_ghost", decision="approved")


# --- service: validation issues -----------------------------------------------------


def test_resolve_issue_persists_status_and_audit(session, data):
    resolve_issue(
        session, issue_id=data.issue.id, status="resolved", reviewer_notes="60m is the figure"
    )
    session.commit()
    assert data.issue.review_status == "resolved"
    row = session.query(m.Review).filter_by(target_id=data.issue.id).one()
    assert row.decision == "resolved"
    assert row.target_type == "validation_issue"


def test_resolve_unknown_or_bad_status_raises(session, data):
    with pytest.raises(ReviewNotFoundError, match="does not exist"):
        resolve_issue(session, issue_id="issue_ghost", status="resolved")
    with pytest.raises(ReviewInputError, match="one of"):
        resolve_issue(session, issue_id=data.issue.id, status="approved")


# --- export gate persists (Step 6/8 interplay) ---------------------------------------


def test_export_approval_still_blocked_with_flagged_review_left_open(session, data):
    review_claim(session, claim_id=data.claim_1.id, decision="flagged")
    review_claim(session, claim_id=data.claim_2.id, decision="approved")
    review_hypothesis(session, hypothesis_id=data.hypothesis.id, decision="approved")
    resolve_issue(session, issue_id=data.issue.id, status="waived")
    session.commit()
    with pytest.raises(ApprovalBlocked, match="still await review"):
        approve_run(session, data.run)


def test_full_review_unblocks_export_approval(session, data):
    review_claim(session, claim_id=data.claim_1.id, decision="approved")
    review_claim(session, claim_id=data.claim_2.id, decision="rejected")
    review_hypothesis(session, hypothesis_id=data.hypothesis.id, decision="approved")
    resolve_issue(session, issue_id=data.issue.id, status="resolved")
    session.commit()
    approve_run(session, data.run)
    assert data.run.status == "approved"


# --- review API ----------------------------------------------------------------------


def test_review_payload_lists_unresolved_and_provenance(session, client, data):
    payload = client.get(f"/runs/{data.run.id}/review").json()
    assert payload["unresolved"] == {"pending_claims": 2, "open_hypotheses": 1, "pending_issues": 1}
    claim_payload = next(c for c in payload["claims"] if c["claim_id"] == data.claim_1.id)
    assert claim_payload["supporting_excerpt"].startswith("acquired Meridian")
    assert claim_payload["sources"][0]["publisher"] == "Trade Press"
    issue_payload = next(h for h in payload["hypotheses"])
    assert issue_payload["status"] == "draft"


def test_review_payload_unknown_run_is_404(session, client):
    assert client.get("/runs/run_ghost/review").status_code == 404


def test_post_review_roundtrip_via_api(session, client, data):
    response = client.post(
        "/reviews",
        json={
            "target_type": "claim",
            "target_id": data.claim_1.id,
            "decision": "approved",
            "reviewer_notes": "looks right",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["decision"] == "approved"
    assert body["target_id"] == data.claim_1.id
    assert body["created_at"]
    assert data.claim_1.review_status == "approved"


def test_post_review_malformed_and_unknown(session, client, data):
    assert (
        client.post(
            "/reviews",
            json={"target_type": "claim", "target_id": data.claim_1.id, "decision": "publish"},
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/reviews",
            json={"target_type": "claim", "target_id": "claim_ghost", "decision": "approved"},
        ).status_code
        == 404
    )
    bad_edit = client.post(
        "/reviews",
        json={"target_type": "claim", "target_id": data.claim_1.id, "decision": "edited"},
    )
    assert bad_edit.status_code == 400
    wrong_router = client.post(
        "/reviews",
        json={
            "target_type": "validation_issue",
            "target_id": data.issue.id,
            "decision": "approved",
        },
    )
    assert wrong_router.status_code == 400
    assert "validation-issues" in wrong_router.json()["detail"]


def test_issue_status_endpoint(session, client, data):
    resolved = client.post(
        f"/validation-issues/{data.issue.id}/status",
        json={"status": "resolved", "reviewer_notes": "confirmed 60m"},
    )
    assert resolved.status_code == 200
    assert resolved.json()["review_status"] == "resolved"
    assert (
        client.post(
            "/validation-issues/issue_ghost/status", json={"status": "resolved"}
        ).status_code
        == 404
    )


def test_runs_listing(session, client, data):
    payload = client.get("/runs").json()
    entries = [run for run in payload["runs"] if run["run_id"] == data.run.id]
    assert len(entries) == 1
    assert entries[0]["status"] == "awaiting_review"
