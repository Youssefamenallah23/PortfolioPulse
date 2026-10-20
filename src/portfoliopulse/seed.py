"""Development seed: a small FICTIONAL fixture graph for offline demos and tests.

Everything here is invented. Domains are reserved .test names; do not present
this data as a real company, a real acquisition, or real figures.
"""

from portfoliopulse import models as m
from portfoliopulse.db import utcnow


def seed_demo(session, *, explicit_ids: bool = True) -> dict[str, str]:
    """Create Company → ResearchRun → 2 Sources → Claims → Hypothesis → nothing approved.

    Returns the IDs of the created records. The run stays in `awaiting_review`;
    nothing is approved — that requires a persisted human review (Step 8).
    """

    def pid(prefix: str, index: int) -> str:
        return f"{prefix}_demo_{index:03d}" if explicit_ids else m.new_id(prefix)

    company_id = pid("company", 1)
    run_id = pid("run", 1)
    src1, src2 = pid("src", 1), pid("src", 2)
    claim1 = pid("claim", 1)
    hyp1 = pid("hyp", 1)

    company = m.Company(
        id=company_id,
        name="Northstar Components (fixture)",
        website="https://www.northstar-components.test",
        sector="Industrial components",
    )
    session.add(company)

    run = m.ResearchRun(
        id=run_id,
        company_id=company_id,
        research_focus="Acquisition integration and geographic expansion (fixture)",
        status="awaiting_review",
        config_summary={
            "search_provider": "mock",
            "llm_provider": "mock",
            "max_sources_per_run": 20,
        },
    )
    session.add(run)

    source1, _ = m.ingest_source(
        session,
        run_id,
        company_id,
        source_id=src1,
        url="https://www.northstar-components.test/investors/news/acquisition",
        canonical_url="https://www.northstar-components.test/investors/news/acquisition",
        content_hash="sha256:fixture-acquisition-hash",
        title="Acquisition announcement (fixture)",
        publisher="northstar-components.test",
        source_type="official_company_announcement",
        http_status=200,
        status="fetched",
        excerpt="Northstar announced it has acquired a regional distributor (fixture excerpt).",
    )

    source2, _ = m.ingest_source(
        session,
        run_id,
        company_id,
        source_id=src2,
        url="https://trade-news.test/northstar-expands?utm_source=fixture",
        canonical_url="https://trade-news.test/northstar-expands",
        content_hash="sha256:fixture-expansion-hash",
        title="Trade press on regional expansion (fixture)",
        publisher="trade-news.test",
        source_type="news",
        http_status=200,
        status="fetched",
        excerpt="Trade press reports Northstar entered two new regions (fixture excerpt).",
    )

    claim = m.Claim(
        id=claim1,
        run_id=run_id,
        company_id=company_id,
        claim_text="The company announced an acquisition of a regional distributor.",
        category="acquisition",
        supporting_excerpt="Northstar announced it has acquired a regional distributor (fixture excerpt).",
        event_date=None,
    )
    session.add(claim)
    m.link_claim_source(session, claim, source1)
    m.set_claim_verified(session, claim)

    hypothesis = m.Hypothesis(
        id=hyp1,
        run_id=run_id,
        company_id=company_id,
        title="Potential integration complexity (fixture)",
        hypothesis_text="The acquisition and geographic expansion activity may increase "
        "coordination demands across business units (fixture hypothesis).",
        alternative_explanations=[
            "The company may already have a mature integration function.",
            "The acquired business may intentionally operate independently.",
        ],
        missing_evidence=["Current operating model and decision rights (fixture)"],
        questions_to_validate=["Who owns cross-business integration decisions? (fixture)"],
        status="draft",
    )
    session.add(hypothesis)
    hypothesis.claims.append(claim)  # ORM-managed junction insert (ordering handled)
    session.flush()

    run.source_count = 2
    run.claim_count = 1
    run.hypothesis_count = 1
    session.flush()

    return {
        "company_id": company.id,
        "run_id": run.id,
        "source_ids": [source1.id, source2.id],
        "claim_id": claim.id,
        "hypothesis_id": hypothesis.id,
        "seeded_at": utcnow().isoformat() + "Z",
    }
