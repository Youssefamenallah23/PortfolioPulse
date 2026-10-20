"""Step 6 acceptance tests: full pipeline, failure paths, idempotency, approval gate.

Every path runs on fixtures/mock providers with zero network and zero API keys.
"""

import json

import pytest

from portfoliopulse import models as m
from portfoliopulse.collect import FetchOutcome, MockFetcher
from portfoliopulse.extract import MockClaimExtractor
from portfoliopulse.models import record_review
from portfoliopulse.pipeline import ApprovalBlocked, ResearchPipeline, approve_run
from portfoliopulse.validate import ISSUE_CONFLICTING

# end-to-end fixture: an M&A + expansion story with per-source details
S1 = "<p>Northstar acquired Meridian for approximately 48 million euros, announced on 2026-07-14.</p> <p>Northstar also acquired Talos Logistics in the same quarter.</p>"
S2 = "<p>Northstar announced entry into two new regions this year, according to the company.</p> <p>Northstar will open a third regional hub in 2027.</p>"

CLAIM_FIXTURES = {
    "acquired Meridian for approximately 48 million": json.dumps(
        {
            "claims": [
                {
                    "claim_text": "Northstar acquired Meridian for approximately 48 million euros.",
                    "category": "acquisition",
                    "supporting_excerpt": "acquired Meridian for approximately 48 million",
                },
                {
                    "claim_text": "Northstar acquired Talos Logistics in the same quarter.",
                    "category": "acquisition",
                    "supporting_excerpt": "acquired Talos Logistics in the same quarter",
                },
            ],
        }
    ),
    "entry into two new regions": json.dumps(
        {
            "claims": [
                {
                    "claim_text": "Northstar entered two new regions this year.",
                    "category": "geographic_expansion",
                    "supporting_excerpt": "entry into two new regions",
                },
                {
                    "claim_text": "Northstar will open a third regional hub in 2027.",
                    "category": "geographic_expansion",
                    "supporting_excerpt": "open a third regional hub in 2027",
                },
            ],
        }
    ),
}


def make_run(session, *, website=None):
    company = m.Company(
        id=m.new_id("company"), name="Northstar Components (fixture)", website=website
    )
    session.add(company)
    run = m.ResearchRun(
        id=m.new_id("run"),
        company_id=company.id,
        research_focus="acquisition integration",
        status="queued",
    )
    session.add(run)
    session.flush()
    return company, run


def make_pipeline(session, company, run, fetcher, *, extractor=None, search=None, max_rounds=2):
    search = search or RoundSearchProvider(
        [[_SearchHit("https://news.example.test/s1"), _SearchHit("https://news.example.test/s2")]]
    )
    return ResearchPipeline(
        session,
        run,
        company,
        search_provider=search,
        extractor=extractor or MockClaimExtractor(CLAIM_FIXTURES),
        fetcher=fetcher,
        max_research_rounds=max_rounds,
    )


class _SearchHit:
    """Minimal SearchHit-like object without importing the dataclass everywhere."""

    def __init__(self, url, title=None, source_type=None):
        self.url, self.title, self.source_type = url, title, source_type


class RoundSearchProvider:
    """Search provider that yields a different result list per call (for round tests)."""

    provider_name = "mock"

    def __init__(self, per_round: list[list[_SearchHit]]):
        self.per_round = per_round
        self.calls = 0

    def search(self, company, focus, limit):
        self.calls += 1
        idx = min(self.calls - 1, len(self.per_round) - 1)
        return self.per_round[idx][:limit]


def default_fetcher():
    return MockFetcher(
        {
            "https://news.example.test/s1": [FetchOutcome(200, "text/html", S1.encode(), "…")],
            "https://news.example.test/s2": [FetchOutcome(200, "text/html", S2.encode(), "…")],
        }
    )


# --- happy path -------------------------------------------------------------------


def test_happy_path_full_pipeline(session):
    company, run = make_run(session)
    provider = RoundSearchProvider(
        [[_SearchHit("https://news.example.test/s1"), _SearchHit("https://news.example.test/s2")]]
    )
    state = make_pipeline(session, company, run, default_fetcher(), search=provider).execute()
    session.commit()

    assert run.status == "awaiting_review"  # draft until a human decides
    assert state.error is None
    summary = state.summary
    assert summary["sources_collected"] == 2
    assert summary["claims_supported"] >= 2
    assert summary["hypotheses_drafted"] == 2  # one per evidence theme with 2+ claims
    assert summary["exports"] == []
    assert "collection:" in state.warnings[0]
    # node trace shows the whole graph execution in order (annotated with counts)
    assert [t.split("(", 1)[0] for t in state.trace] == [
        "start",
        "validate_request",
        "plan_research",
        "collect_sources",
        "extract_claims",
        "validate_claims",
        "generate_hypotheses",
        "review_pending",
        "finalize",
    ]
    # drafts cannot be approved without review
    with pytest.raises(ApprovalBlocked) as excinfo:
        approve_run(session, run)
    assert "still await review" in str(excinfo.value)


def test_full_run_reaches_approved_after_review(session):
    company, run = make_run(session)
    provider = RoundSearchProvider(
        [[_SearchHit("https://news.example.test/s1"), _SearchHit("https://news.example.test/s2")]]
    )
    make_pipeline(session, company, run, default_fetcher(), search=provider).execute()

    for claim in session.query(m.Claim).filter_by(run_id=run.id).all():
        record_review(session, target_type="claim", target_id=claim.id, decision="approved")
    for hyp in session.query(m.Hypothesis).filter_by(run_id=run.id).all():
        record_review(session, target_type="hypothesis", target_id=hyp.id, decision="approved")

    approve_run(session, run)  # no exception
    assert run.status == "approved"
    assert run.finished_at is not None


# --- failure and gap paths --------------------------------------------------------


def test_no_sources_found_fails_honestly(session):
    company, run = make_run(session)
    from portfoliopulse.collect import MockSearchProvider

    state = make_pipeline(
        session, company, run, MockFetcher({}), search=MockSearchProvider([])
    ).execute()
    session.commit()
    assert run.status == "failed"
    assert "no usable sources found" in (run.error_summary or "")
    assert state.error


def test_failed_sources_are_visible_not_faked(session):
    company, run = make_run(session)
    fetcher = MockFetcher(
        {
            "https://news.example.test/s1": [FetchOutcome(200, "text/html", S1.encode(), "…")],
            "https://news.example.test/s2": [FetchOutcome(403, "text/html", None, "…")],
            "https://news.example.test/s3": [FetchOutcome(None, None, None, "…", error="timeout")],
        }
    )
    provider = RoundSearchProvider(
        [
            [
                _SearchHit("https://news.example.test/s1"),
                _SearchHit("https://news.example.test/s2"),
                _SearchHit("https://news.example.test/s3"),
            ]
        ]
    )
    make_pipeline(session, company, run, fetcher, search=provider).execute()
    session.commit()
    assert run.status == "awaiting_review"
    statuses = {s.status for s in session.query(m.Source).filter_by(run_id=run.id).all()}
    assert statuses == {"fetched", "blocked", "failed"}


def test_all_claims_fail_validation_gap_report(session):
    company, run = make_run(session)
    bad = json.dumps(
        {
            "claims": [
                {
                    "claim_text": "A claim whose excerpt cannot exist in any source text.",
                    "category": "acquisition",
                    "supporting_excerpt": "excerpt that simply is not in the page",
                }
            ]
        }
    )
    # both sources' fixture markers matched but excerpt never present
    fetcher = default_fetcher()
    provider = RoundSearchProvider(
        [[_SearchHit("https://news.example.test/s1"), _SearchHit("https://news.example.test/s2")]]
    )
    # replace extractor with one that steps out through claim_via fixtures but excerpt bad
    extractor = MockClaimExtractor(
        {
            "acquired Meridian": bad,
            "entry into two new regions": bad,
        }
    )
    state = make_pipeline(
        session, company, run, fetcher, extractor=extractor, search=provider, max_rounds=1
    ).execute()
    session.commit()
    assert run.status == "awaiting_review"
    assert state.summary["claims_supported"] == 0
    assert state.summary["hypotheses_drafted"] == 0
    assert any("insufficient supported evidence" in w for w in state.warnings)


def test_contradictory_sources_reach_review_with_open_conflict(session):
    company, run = make_run(session)
    t_a = "<p>Northstar acquired Meridian for approximately 48 million euros.</p>"
    t_b = "<p>Trade press says the Northstar Meridian deal was worth 60 million euros.</p>"
    fetcher = MockFetcher(
        {
            "https://news.example.test/s1": [FetchOutcome(200, "text/html", t_a.encode(), "…")],
            "https://news.example.test/s2": [FetchOutcome(200, "text/html", t_b.encode(), "…")],
        }
    )

    class FigProvider:
        provider_name = "mock"

        def __init__(self):
            self.calls = 0

        def search(self, c, f, limit):
            self.calls += 1
            return (
                [_SearchHit("https://news.example.test/s1")]
                if self.calls == 1
                else [_SearchHit("https://news.example.test/s2")]
            )

    fixtures = {
        "acquired Meridian for approximately 48 million": json.dumps(
            {
                "claims": [
                    {
                        "claim_text": "Northstar acquired Meridian for 48 million euros.",
                        "category": "acquisition",
                        "supporting_excerpt": "acquired Meridian for approximately 48 million euros",
                    }
                ]
            }
        ),
        "deal was worth 60 million": json.dumps(
            {
                "claims": [
                    {
                        "claim_text": "The Northstar Meridian deal was worth 60 million euros.",
                        "category": "acquisition",
                        "supporting_excerpt": "deal was worth 60 million euros",
                    }
                ]
            }
        ),
    }
    state = make_pipeline(
        session, company, run, fetcher, extractor=MockClaimExtractor(fixtures), search=FigProvider()
    ).execute()
    session.commit()
    assert run.status == "awaiting_review"
    open_conflicts = (
        session.query(m.ValidationIssue)
        .filter_by(run_id=run.id, issue_type=ISSUE_CONFLICTING, review_status="pending")
        .count()
    )
    assert open_conflicts == 1  # one issue for the conflicting pair
    assert state.summary["open_conflicts"] == 2  # both claims in the pair flagged
    # neither side of an unresolved conflict is 'supported'
    assert {
        c.verification_status for c in session.query(m.Claim).filter_by(run_id=run.id).all()
    } == {"conflicting"}


def test_llm_provider_error_does_not_erase_partial_results(session):
    company, run = make_run(session)

    class ExplodingExtractor:
        provider_name = "mock"

        def extract(self, *a, **k):
            raise RuntimeError("LLM provider credentials expired")

    provider = RoundSearchProvider([[_SearchHit("https://news.example.test/s1")]])
    make_pipeline(
        session, company, run, default_fetcher(), extractor=ExplodingExtractor(), search=provider
    ).execute()
    session.commit()
    assert run.status == "failed"
    assert "provider" in (run.error_summary or "")
    # collected sources persist for debugging and resume
    assert session.query(m.Source).filter_by(run_id=run.id).count() == 1


def test_max_research_rounds_bounded_then_gap_report(session):
    company, run = make_run(session)
    # only ONE supported claim across the run → below the 2-claim threshold →
    # a second (bounded) round happens, same evidence, then max-rounds gap report
    provider = RoundSearchProvider(
        [
            [_SearchHit("https://news.example.test/s1")],
            [_SearchHit("https://news.example.test/s1")],  # same URL: dedupe, no new sources
        ]
    )
    fixtures = {
        "acquired Meridian for approximately 48 million": json.dumps(
            {
                "claims": [
                    {
                        "claim_text": "Northstar acquired Meridian for approximately 48 million euros.",
                        "category": "acquisition",
                        "supporting_excerpt": "acquired Meridian for approximately 48 million",
                    }
                ]
            }
        ),
    }
    state = make_pipeline(
        session,
        company,
        run,
        default_fetcher(),
        extractor=MockClaimExtractor(fixtures),
        search=provider,
        max_rounds=2,
    ).execute()
    session.commit()
    assert state.round == 2
    assert run.status == "awaiting_review"
    assert any("max research rounds" in w for w in state.warnings)
    assert len([t for t in state.trace if t.startswith("collect_sources")]) == 2


# --- interruption / resume with idempotency ---------------------------------------


def test_resume_after_interruption_creates_no_duplicates(tmp_path):
    from portfoliopulse.db import make_engine, make_session_factory

    engine = make_engine(f"sqlite:///{tmp_path / 'resume.db'}")
    s1 = next(iter([make_session_factory(engine)()]))
    company, run = make_run(s1)
    provider = RoundSearchProvider(
        [[_SearchHit("https://news.example.test/s1"), _SearchHit("https://news.example.test/s2")]]
    )

    first = ResearchPipeline(
        s1,
        run,
        company,
        search_provider=provider,
        extractor=MockClaimExtractor(CLAIM_FIXTURES),
        fetcher=default_fetcher(),
    )

    # destroy the LLM mid-way: collect succeeds, extraction explodes, run marked failed
    class Boom:
        provider_name = "mock"

        def extract(self, *a, **k):
            raise RuntimeError("simulated crash mid-pipeline")

    first.extractor = Boom()
    first.execute()
    s1.commit()
    s1.close()

    # ...resume in a fresh session with a working provider
    s2 = next(iter([make_session_factory(engine)()]))
    run2 = s2.get(m.ResearchRun, run.id)
    company2 = s2.get(m.Company, company.id)
    provider2 = RoundSearchProvider(
        [[_SearchHit("https://news.example.test/s1"), _SearchHit("https://news.example.test/s2")]]
    )
    ResearchPipeline(
        s2,
        run2,
        company2,
        search_provider=provider2,
        extractor=MockClaimExtractor(CLAIM_FIXTURES),
        fetcher=MockFetcher({}),  # nothing new to fetch from the network
    ).execute()
    s2.commit()

    assert run2.status == "awaiting_review"
    sources = s2.query(m.Source).filter_by(run_id=run.id).all()
    assert len(sources) == 2  # NO duplicate source rows after resume
    claims = s2.query(m.Claim).filter_by(run_id=run.id).all()
    assert len(claims) == 4  # claims extracted once, not twice
    assert all(s.extraction_status == "extracted" for s in sources)

    # a full THIRD pass must skip already-extracted sources entirely (idempotent resume)
    s3 = make_session_factory(engine)()
    run3 = s3.get(m.ResearchRun, run.id)
    company3 = s3.get(m.Company, company.id)
    state3 = ResearchPipeline(
        s3,
        run3,
        company3,
        search_provider=RoundSearchProvider(
            [
                [
                    _SearchHit("https://news.example.test/s1"),
                    _SearchHit("https://news.example.test/s2"),
                ]
            ]
        ),
        extractor=MockClaimExtractor(CLAIM_FIXTURES),
        fetcher=MockFetcher({}),
    ).execute()
    s3.commit()
    assert s3.query(m.Claim).filter_by(run_id=run.id).count() == 4  # still no duplicates
    assert s3.query(m.Source).filter_by(run_id=run.id).count() == 2
    assert any("already extracted" in w for w in state3.warnings)


# --- validation guards ------------------------------------------------------------


def test_invalid_website_fails_request_validation(session):
    company, run = make_run(session, website="not a url")
    state = make_pipeline(session, company, run, MockFetcher({})).execute()
    session.commit()
    assert run.status == "failed"
    assert "website" in (state.error or "")


def test_empty_research_focus_fails(session):
    company, run = make_run(session)
    run.research_focus = "  "
    state = make_pipeline(session, company, run, MockFetcher({})).execute()
    session.commit()
    assert run.status == "failed"
    assert "focus" in (state.error or "")
