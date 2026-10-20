"""Offline demo mode: fixture search + fetch + extraction over FICTIONAL companies.

All domains are reserved `.test` names and all data is invented. This exists so
the complete workbench flow (create run → review → approve → export) can run
end-to-end with zero network access and zero API keys. It is selected with
SEARCH_PROVIDER=demo / LLM_PROVIDER=demo (the fetcher follows the search mode).
"""

from __future__ import annotations

import json

from portfoliopulse.collect import (
    FetchOutcome,
    MockFetcher,
    MockSearchProvider,
    SearchHit,
)
from portfoliopulse.extract import MockClaimExtractor

URL_PROFILE = "https://www.northstar-components.test"
URL_ACQUISITION = "https://www.northstar-components.test/investors/news/acquisition-meridian"
URL_RESULTS = "https://www.northstar-components.test/investors/annual-results"
URL_EXPANSION = "https://trade-news.test/northstar-expansion"

# Sentences that carry fixtures stay on one line: excerpts are matched as
# literal substrings of the stored page text.
PAGE_PROFILE = (
    "Northstar Components — company site (fixture)\n\n"
    "Northstar Components is a supplier of industrial sensor components. "
    "The company profile lists roughly 850 employees and a Hamburg head office. "
    "This fixture page exists only to keep the demo offline; everything here is fictional."
)

PAGE_ACQUISITION = (
    "Investor news (fixture): acquisition of Meridian Messtechnik completed\n\n"
    "On 2026-07-14 Northstar announced the completed acquisition of Meridian Messtechnik, "
    "a regional distributor of measurement equipment. "
    "The purchase price is reported at 48 million euros. "
    "Integration of the acquired business into the components division starts this quarter."
)

PAGE_EXPANSION = (
    "Trade news (fixture): Northstar keeps widening its footprint\n\n"
    "Northstar entered two new regions this year, according to the company. "
    "The article mentions a sales office in Rotterdam and a small warehouse. "
    "Separately, trade press reports value the Meridian deal at around 60 million euros. "
    "No other figures are stated in this fixture."
)

PAGE_RESULTS = (
    "Investor news (fixture): annual results 2025\n\n"
    "In its annual results Northstar reported revenue of 312 million euros for 2025, "
    "up 14 percent from the prior year. "
    "Every figure on this fixture page is fictional."
)

MARKER_PROFILE = "supplier of industrial sensor components"
MARKER_ACQUISITION = "completed acquisition of Meridian Messtechnik"
MARKER_EXPANSION = "entered two new regions this year"
MARKER_RESULTS = "reported revenue of 312 million euros"


def _claims() -> dict[str, str]:
    """Marker → extractor JSON. Excerpts occur verbatim in the matching page text."""
    profile = {
        "claims": [
            {
                "claim_text": "Northstar Components is a supplier of industrial sensor components.",
                "category": "company_profile",
                "supporting_excerpt": "Northstar Components is a supplier of industrial sensor components.",
                "event_date": None,
            }
        ]
    }
    acquisition = {
        "claims": [
            {
                "claim_text": "Northstar announced the completed acquisition of Meridian Messtechnik.",
                "category": "acquisition",
                "supporting_excerpt": "Northstar announced the completed acquisition of Meridian Messtechnik",
                "event_date": "2026-07-14",
            },
            {
                "claim_text": "The purchase price is reported at 48 million euros.",
                "category": "acquisition",
                "supporting_excerpt": "The purchase price is reported at 48 million euros.",
                "event_date": None,
            },
        ]
    }
    expansion = {
        "claims": [
            {
                "claim_text": "According to the company, Northstar entered two new regions this year.",
                "category": "geographic_expansion",
                "supporting_excerpt": "Northstar entered two new regions this year, according to the company.",
                "event_date": None,
            },
            {
                "claim_text": "Northstar opened a sales office in Rotterdam.",
                "category": "geographic_expansion",
                "supporting_excerpt": "The article mentions a sales office in Rotterdam",
                "event_date": None,
            },
            {
                "claim_text": "Trade press reports value the Meridian deal at around 60 million euros.",
                "category": "acquisition",
                "supporting_excerpt": "trade press reports value the Meridian deal at around 60 million euros",
                "event_date": None,
            },
        ]
    }
    results = {
        "claims": [
            {
                "claim_text": "Northstar reported revenue of 312 million euros for 2025, up 14 percent.",
                "category": "financial_performance",
                "supporting_excerpt": "Northstar reported revenue of 312 million euros for 2025, up 14 percent",
                "event_date": None,
            }
        ]
    }
    return {
        MARKER_PROFILE: json.dumps(profile),
        MARKER_ACQUISITION: json.dumps(acquisition),
        MARKER_EXPANSION: json.dumps(expansion),
        MARKER_RESULTS: json.dumps(results),
    }


PAGES: dict[str, str] = {
    URL_PROFILE: PAGE_PROFILE,
    URL_ACQUISITION: PAGE_ACQUISITION,
    URL_RESULTS: PAGE_RESULTS,
    URL_EXPANSION: PAGE_EXPANSION,
}

SEARCH_HITS = [
    SearchHit(
        url=URL_ACQUISITION,
        title="Acquisition of Meridian (fixture)",
        source_type="official_company_announcement",
    ),
    SearchHit(
        url=URL_RESULTS,
        title="Annual results 2025 (fixture)",
        source_type="official_company_announcement",
    ),
    SearchHit(url=URL_EXPANSION, title="Trade news on expansion (fixture)", source_type="news"),
]


class DemoSearchProvider(MockSearchProvider):
    provider_name = "demo"

    def __init__(self):
        super().__init__(SEARCH_HITS)


class DemoClaimExtractor(MockClaimExtractor):
    provider_name = "demo"

    def __init__(self):
        super().__init__(_claims())


def demo_fetcher() -> MockFetcher:
    """Every demo page resolves as a 200 HTML page; unknown URLs fail without network."""
    outcomes = {
        url: [FetchOutcome(200, "text/html", text.encode("utf-8"), url)]
        for url, text in PAGES.items()
    }
    return MockFetcher(outcomes)
