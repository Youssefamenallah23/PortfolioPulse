"""Evaluation fixture dataset: 10 FICTIONAL companies in one industry (industrial
components), each with its own fixture pages. All reserved `.test` domains; all
data invented. Nothing here is a real company, acquisition, or figure.

Purpose: a deterministic, offline dataset for the Step 11 metric harness. Gold
labels encode what a careful human annotator would conclude for each claim. The
traps are deliberate and few (documented in docs/evaluation.md):
- 2 "partly supported" claims (the supporting excerpt says "approximately", the
  extracted wording drops the qualifier).
- 1 prose-form date ("On 14 July 2026") the system's strict-ISO-date rule
  deliberately refuses to parse → missing event_date.
- 1 claim whose supporting excerpt does not exist in the page (gold: unsupported).
- 1 company with two pages reporting different purchase prices (gold: conflicting).
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from portfoliopulse.collect import FetchOutcome, MockFetcher, MockSearchProvider, SearchHit

ACQ_MARKER = "completed acquisition of a regional logistics partner"
EXPANSION_MARKER = "entered two new regions this year"
TRAP_MARKER = "routine"


@dataclass(frozen=True)
class GoldClaim:
    claim_text: str
    label: str  # supported | partly | unsupported | incorrect | conflicting
    gold_date: str | None  # the date the SOURCE actually states, ISO if any


@dataclass(frozen=True)
class CompanyCase:
    slug: str
    name: str
    pages: dict[str, str]  # url -> page text
    hits: list[SearchHit]
    extractor_fixtures: dict[str, str]  # marker -> claims JSON
    gold: list[GoldClaim]


def _claim(claim_text: str, category: str, excerpt: str, event_date: str | None) -> dict:
    return {
        "claim_text": claim_text,
        "category": category,
        "supporting_excerpt": excerpt,
        "event_date": event_date,
    }


def _pack(claims: list[dict]) -> str:
    return json.dumps({"claims": claims})


def company_case(
    slug: str,
    name: str,
    *,
    price: int,
    iso_date: str | None,  # None → page states a prose date (strict-ISO trap)
    price_approx: bool = False,  # "partly supported" trap (drops "approximately")
    unsupported_trap: bool = False,  # excerpt nowhere in the page
    conflicting_prices: tuple[int, int] | None = None,  # (this page, other page)
) -> CompanyCase:
    host = f"https://www.{slug}.fixture.test"
    page_date = iso_date if iso_date else "14 July 2026"
    gold_date = iso_date if iso_date else "2026-07-14"
    price_wording = (
        f"The purchase price is reported at approximately {price} million euros."
        if price_approx
        else f"The purchase price is reported at {price} million euros."
    )
    news_url = f"{host}/investors/news/acquisition"
    expansion_url = f"{host}/news/expansion"
    acq_claim_text = f"{name} announced the completed acquisition of a regional logistics partner."
    price_claim_text = f"The purchase price is reported at {price} million euros."
    expansion_claim_text = f"According to the company, {name} entered two new regions this year."

    news_page = (
        f"Investor news (fixture): {name} acquisition completed\n\n"
        f"On {page_date} {name} announced the completed acquisition of a regional "
        f"logistics partner. "
        f"{price_wording} "
        "Integration of the acquired business into the components division starts this quarter."
    )
    expansion_page = (
        f"Trade news (fixture): {name} keeps widening its footprint\n\n"
        f"{name} entered two new regions this year, according to the company. "
        "The article mentions a sales office and a small warehouse. No figures are stated."
    )
    pages = {news_url: news_page, expansion_url: expansion_page}
    hits = [
        SearchHit(
            url=news_url,
            title="Acquisition news (fixture)",
            source_type="official_company_announcement",
        ),
        SearchHit(url=expansion_url, title="Trade news (fixture)", source_type="news"),
    ]

    fixtures: dict[str, str] = {
        ACQ_MARKER: _pack(
            [
                _claim(
                    acq_claim_text,
                    "acquisition",
                    "announced the completed acquisition of a regional logistics partner",
                    iso_date,  # strict rule: prose dates are kept out (gold still knows the date)
                ),
                _claim(price_claim_text, "acquisition", price_wording, None),
            ]
        ),
        EXPANSION_MARKER: _pack(
            [
                _claim(
                    expansion_claim_text,
                    "geographic_expansion",
                    f"{name} entered two new regions this year, according to the company.",
                    None,
                )
            ]
        ),
    }
    gold = [
        GoldClaim(acq_claim_text, "supported", gold_date),
        GoldClaim(price_claim_text, "partly" if price_approx else "supported", None),
        GoldClaim(expansion_claim_text, "supported", None),
    ]

    if unsupported_trap:
        # claim whose excerpt exists nowhere in the page — the deterministic
        # validator flags it (excerpt_not_found); strict review rejects it
        trap_claim = _claim(
            f"{name} is exiting the European market entirely.",
            "strategy",
            "exiting the European market entirely",
            None,
        )
        pages[news_url] = (
            news_page + " The company statement closes with routine boilerplate this quarter."
        )
        fixtures[TRAP_MARKER] = _pack([trap_claim])
        gold.append(GoldClaim(trap_claim["claim_text"], "unsupported", None))

    if conflicting_prices is not None:
        other_price = f"The purchase price is reported at {conflicting_prices[1]} million euros."
        strategy_claim_text = (
            f"{name} has reportedly widened its footprint across two neighbouring countries."
        )
        blog_page = (
            f"Analyst blog (fixture): scrutiny of the {name} deal\n\n"
            f"The reported figure differs from the company statement here: the same "
            f"transaction is reported at {conflicting_prices[1]} million euros in this "
            f"analysis. The blog adds that {name} has reportedly widened its footprint "
            "across two neighbouring countries."
        )
        blog_url = f"{host}/analysis/price"
        pages[blog_url] = blog_page
        hits.append(SearchHit(url=blog_url, title="Analyst blog (fixture)", source_type="news"))
        # the news page's price claim (this page's figure) is already inside the
        # acquisition fixture — the blog page gets the competing figure:
        fixtures["reported figure differs from the company statement"] = _pack(
            [
                _claim(
                    other_price,
                    "acquisition",
                    f"the same transaction is reported at {conflicting_prices[1]} million euros",
                    None,
                ),
                _claim(
                    strategy_claim_text,
                    "strategy",
                    f"{name} has reportedly widened its footprint across two neighbouring countries.",
                    None,
                ),
            ]
        )
        gold = [g for g in gold if "purchase price" not in g.claim_text]
        gold.append(GoldClaim(price_claim_text, "conflicting", None))
        gold.append(GoldClaim(other_price, "conflicting", None))
        gold.append(GoldClaim(strategy_claim_text, "supported", None))

    return CompanyCase(
        slug=slug,
        name=name,
        pages=pages,
        hits=hits,
        extractor_fixtures=fixtures,
        gold=gold,
    )


def build_company_cases() -> list[CompanyCase]:
    """The 10-company evaluation set (one industry: industrial components)."""
    return [
        company_case("aalborg", "Aalborg Fasteners", price=34, iso_date="2026-02-03"),
        company_case("brno", "Brno Gearworks", price=41, iso_date="2026-03-11"),
        company_case(
            "carmona", "Carmona Pumps", price=48, iso_date="2026-04-17", price_approx=True
        ),
        company_case("dongola", "Dongola Bearings", price=55, iso_date="2026-05-05"),
        company_case("espoo", "Espoo Hydraulics", price=27, iso_date="2026-06-09"),
        company_case(
            "ferrol", "Ferrol Castings", price=62, iso_date="2026-06-23", unsupported_trap=True
        ),
        company_case("gdansk", "Gdansk Valves", price=39, iso_date="2026-07-30", price_approx=True),
        company_case("ilheus", "Ilheus Springs", price=33, iso_date="2026-08-14"),
        company_case("jonkoping", "Jonkoping Sensors", price=46, iso_date=None),  # prose-date trap
        company_case(
            "kaunas",
            "Kaunas Electronics",
            price=60,
            iso_date="2026-09-01",
            conflicting_prices=(60, 42),
        ),
    ]


def search_provider_for(case: CompanyCase) -> MockSearchProvider:
    provider = MockSearchProvider(case.hits)
    provider.provider_name = "evaluation_fixture"
    return provider


def shared_fetcher() -> MockFetcher:
    outcomes: dict[str, list[FetchOutcome]] = {}
    for case in build_company_cases():
        for url, text in case.pages.items():
            outcomes[url] = [FetchOutcome(200, "text/html", text.encode("utf-8"), url)]
    return MockFetcher(outcomes)
