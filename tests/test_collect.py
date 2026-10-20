"""Step 3 acceptance tests: collection pipeline, statuses, limits, politeness.

No test is allowed to reach the live internet: everything runs on a local
httpx.MockTransport or deterministic mock providers.
"""

from types import SimpleNamespace

import httpx
import pytest

from portfoliopulse import models as m
from portfoliopulse.collect import (
    STATUS_BLOCKED,
    STATUS_DUPLICATE,
    STATUS_FAILED,
    STATUS_FETCHED,
    STATUS_UNSUPPORTED,
    DomainThrottle,
    FetchOutcome,
    HttpxPageFetcher,
    MockFetcher,
    MockSearchProvider,
    SearchHit,
    SearchProviderUnavailable,
    UrlTarget,
    canonical_url,
    collect_targets,
    collect_url,
    get_search_provider,
    publisher_of,
)


def make_run(session):
    company = m.Company(id=m.new_id("company"), name="Northstar Components (fixture)")
    session.add(company)
    run = m.ResearchRun(
        id=m.new_id("run"), company_id=company.id, research_focus="fixture focus", status="running"
    )
    session.add(run)
    session.flush()
    return company, run


def httpx_fetcher(handler, attempts=3, sleeper=lambda _s: None):
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return HttpxPageFetcher(client, attempts=attempts, sleeper=sleeper)


# --- URL normalization --------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("HTTPS://WWW.Example.com/A?b=2&a=1", "https://www.example.com/A?a=1&b=2"),
        ("https://example.com/x/?utm_source=n&utm_medium=m", "https://example.com/x/"),
        ("https://example.com:443/page", "https://example.com/page"),
        ("http://example.com:80/page", "http://example.com/page"),
        ("https://example.com/page#news-section", "https://example.com/page"),
        ("https://example.com/p?a=1&b=2", "https://example.com/p?a=1&b=2"),  # sorted
        ("https://example.com", "https://example.com/"),
    ],
)
def test_canonical_url_variants(raw, expected):
    assert canonical_url(raw) == expected


@pytest.mark.parametrize("raw", ["example.com/page", "ftp://example.com/x", "", "not a url"])
def test_canonical_url_rejects_malformed(raw):
    with pytest.raises(ValueError):
        canonical_url(raw)


def test_publisher_of():
    assert publisher_of(canonical_url("https://www.Trade-News.test/x?q=1")) == "trade-news.test"


# --- collection through the real fetcher + MockTransport -----------------------


HTML_BODY = (
    b"<html><head><title>Acquisition</title><script>ignore me</script></head>"
    b"<body><p>Northstar plans to consolidate warehouses.</p></body></html>"
)


def test_collect_success_html(session):
    company, run = make_run(session)

    def handler(request):
        return httpx.Response(
            200, content=HTML_BODY, headers={"Content-Type": "text/html; charset=utf-8"}
        )

    fetcher = httpx_fetcher(handler)
    source, status = collect_url(
        session,
        run,
        company,
        UrlTarget("https://www.example.test/news?a=1&utm_source=x", title="Acquisition"),
        fetcher=fetcher,
        timeout=1.0,
    )
    session.commit()

    assert status == STATUS_FETCHED
    assert source.canonical_url == "https://www.example.test/news?a=1"
    assert source.publisher == "example.test"
    assert source.http_status == 200
    assert source.content_hash and source.content_hash.startswith("sha256:")
    assert source.excerpt and "plans to consolidate" in source.excerpt
    assert "ignore me" not in (source.excerpt or "")
    stored = session.get(m.SourceText, source.id)
    assert stored and "Northstar" in stored.text
    assert run.source_count == 1
    # unknown fields stay honestly empty
    assert source.published_at is None


def test_collect_redirect_followed(session):
    company, run = make_run(session)

    def handler(request):
        if request.url.path == "/old":
            return httpx.Response(302, headers={"Location": "https://example.test/new"})
        return httpx.Response(200, content=HTML_BODY, headers={"Content-Type": "text/html"})

    source, status = collect_url(
        session, run, company, UrlTarget("https://example.test/old"), fetcher=httpx_fetcher(handler)
    )
    session.commit()
    assert status == STATUS_FETCHED
    assert source.http_status == 200


def test_blocked_403_is_recorded_not_retried(session):
    company, run = make_run(session)
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(403, text="forbidden")

    source, status = collect_url(
        session,
        run,
        company,
        UrlTarget("https://example.test/paywalled"),
        fetcher=httpx_fetcher(handler),  # retries available but refusals must not be retried
    )
    session.commit()
    assert status == STATUS_BLOCKED
    assert source.status == STATUS_BLOCKED and source.http_status == 403
    assert len(calls) == 1  # exactly one attempt
    assert session.get(m.SourceText, source.id) is None


def test_404_is_recorded_as_failed(session):
    company, run = make_run(session)
    source, status = collect_url(
        session,
        run,
        company,
        UrlTarget("https://example.test/missing"),
        fetcher=httpx_fetcher(lambda request: httpx.Response(404, text="nope")),
    )
    session.commit()
    assert status == STATUS_FAILED
    assert source.http_status == 404


def test_5xx_retries_with_backoff_then_fails(session):
    company, run = make_run(session)
    calls = []
    sleeps = []

    def handler(request):
        calls.append(1)
        return httpx.Response(503, text="overloaded")

    source, status = collect_url(
        session,
        run,
        company,
        UrlTarget("https://example.test/flaky"),
        fetcher=httpx_fetcher(handler, attempts=3, sleeper=sleeps.append),
    )
    session.commit()
    assert status == STATUS_FAILED
    assert len(calls) == 3  # bounded retries
    assert sleeps == [0.5, 1.0]  # exponential backoff, then gives up
    assert source.status == STATUS_FAILED


def test_timeout_maps_to_failed_with_null_http_status(session):
    company, run = make_run(session)
    calls = []

    def handler(request):
        calls.append(1)
        raise httpx.ConnectTimeout("timed out", request=request)

    source, status = collect_url(
        session,
        run,
        company,
        UrlTarget("https://example.test/slow"),
        fetcher=httpx_fetcher(handler, attempts=2),
    )
    session.commit()
    assert status == STATUS_FAILED
    assert source.http_status is None  # never faked
    assert len(calls) == 2


def test_unsupported_content_type_rejected(session):
    company, run = make_run(session)
    source, status = collect_url(
        session,
        run,
        company,
        UrlTarget("https://example.test/cat.png"),
        fetcher=httpx_fetcher(
            lambda request: httpx.Response(
                200, content=b"\x89PNG", headers={"Content-Type": "image/png"}
            )
        ),
    )
    session.commit()
    assert status == STATUS_UNSUPPORTED
    assert source.status == STATUS_UNSUPPORTED


def test_oversized_body_rejected(session):
    company, run = make_run(session)
    big = HTML_BODY * 5
    _source, status = collect_url(
        session,
        run,
        company,
        UrlTarget("https://example.test/huge"),
        fetcher=httpx_fetcher(
            lambda request: httpx.Response(200, content=big, headers={"Content-Type": "text/html"})
        ),
        max_bytes=10,  # tiny cap for the test
    )
    session.commit()
    assert status == STATUS_UNSUPPORTED
    assert len(big) > 10


def test_malformed_url_recorded_as_failure(session):
    company, run = make_run(session)
    source, status = collect_url(
        session,
        run,
        company,
        UrlTarget("example.com/no-scheme"),
        fetcher=httpx_fetcher(lambda request: httpx.Response(200)),
    )
    session.commit()
    assert status == STATUS_FAILED
    assert source.http_status is None


# --- dedupe + idempotent refresh ----------------------------------------------


def test_duplicate_by_canonical_url_then_by_content_hash(session):
    company, run = make_run(session)
    resp = lambda request: httpx.Response(
        200, content=HTML_BODY, headers={"Content-Type": "text/html"}
    )
    fetcher = httpx_fetcher(resp)

    first, s1 = collect_url(
        session, run, company, UrlTarget("https://example.test/a"), fetcher=fetcher
    )
    again, s2 = collect_url(
        session, run, company, UrlTarget("https://EXAMPLE.test/a?utm_campaign=x"), fetcher=fetcher
    )
    mirror, s3 = collect_url(
        session, run, company, UrlTarget("https://mirror.test/copy"), fetcher=fetcher
    )
    session.commit()

    assert s1 == STATUS_FETCHED
    assert s2 == STATUS_DUPLICATE and again.id == first.id
    assert s3 == STATUS_DUPLICATE and mirror.id == first.id  # identical body at another URL
    assert session.query(m.Source).filter_by(run_id=run.id).count() == 1


def test_previously_failed_url_refreshes_same_row(session):
    company, run = make_run(session)
    calls = {"n": 0}

    def flaky_then_up(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(500, text="down")
        return httpx.Response(200, content=HTML_BODY, headers={"Content-Type": "text/html"})

    source, s_bad = collect_url(
        session,
        run,
        company,
        UrlTarget("https://example.test/recover"),
        fetcher=httpx_fetcher(flaky_then_up, attempts=1),
    )
    source, s_ok = collect_url(
        session,
        run,
        company,
        UrlTarget("https://example.test/recover"),
        fetcher=httpx_fetcher(flaky_then_up, attempts=1),
    )
    session.commit()
    assert s_bad == STATUS_FAILED
    assert s_ok == STATUS_FETCHED and source.status == STATUS_FETCHED
    assert session.get(m.SourceText, source.id) is not None


# --- batch collection + limits --------------------------------------------------


def test_batch_reports_and_source_limit(session):
    company, run = make_run(session)

    def handler(request):
        if "/paywalled" in str(request.url):
            return httpx.Response(403, text="no")
        if "/offline" in str(request.url):
            raise httpx.ConnectTimeout("x", request=request)
        # distinct bodies so nothing hash-dedupes in this test
        return httpx.Response(
            200,
            content=HTML_BODY + b"<p>unique " + str(request.url.path).encode() + b"</p>",
            headers={"Content-Type": "text/html"},
        )

    targets = [
        UrlTarget("https://example.test/1", source_type="official_page"),
        UrlTarget("https://example.test/2"),
        UrlTarget("https://example.test/paywalled"),
        UrlTarget("https://example.test/offline"),
        UrlTarget("https://example.test/3"),  # would exceed the limit of 2 new sources
    ]
    report = collect_targets(
        session,
        run,
        company,
        targets,
        fetcher=httpx_fetcher(handler, attempts=1),
        timeout=1.0,
        max_sources_per_run=2,
    )
    session.commit()
    totals = report.totals()
    assert totals["fetched"] == 2
    assert totals["blocked"] == 0 and totals["failed"] == 0  # limit hits before those targets
    assert totals["truncated_by_limit"] == 3
    assert run.source_count == 2


def test_batch_records_all_failure_kinds(session):
    company, run = make_run(session)

    def handler(request):
        if "/paywalled" in str(request.url):
            return httpx.Response(403, text="no")
        if "/offline" in str(request.url):
            raise httpx.ConnectTimeout("x", request=request)
        if "/cat.png" in str(request.url):
            return httpx.Response(200, content=b"\x89PNG", headers={"Content-Type": "image/png"})
        return httpx.Response(200, content=HTML_BODY, headers={"Content-Type": "text/html"})

    targets = [
        UrlTarget("https://example.test/dup"),
        UrlTarget("https://example.test/year?utm=x"),  # different URL, same body -> hash duplicate
        UrlTarget("https://example.test/paywalled"),
        UrlTarget("https://example.test/offline"),
        UrlTarget("https://example.test/cat.png"),
    ]
    report = collect_targets(
        session,
        run,
        company,
        targets,
        fetcher=httpx_fetcher(handler, attempts=1),
        timeout=1.0,
        max_sources_per_run=20,
    )
    session.commit()
    totals = report.totals()
    assert totals["fetched"] == 1 and totals["duplicates"] == 1
    assert totals["blocked"] == 1 and totals["failed"] == 1 and totals["unsupported"] == 1
    assert run.source_count == 4  # failures are visible in the registry too


# --- politeness throttle --------------------------------------------------------


def test_throttle_spaces_same_domain_requests():
    tick = iter(range(1, 1000))
    clock = lambda: float(next(tick))
    sleeps: list[float] = []
    throttle = DomainThrottle(min_interval=10, clock=clock, sleeper=sleeps.append)

    assert throttle.wait("example.test") == 0.0  # first visit to the domain waits
    assert throttle.wait("example.test") == 9.0  # 1 tick elapsed; must sleep 9
    assert sleeps == [9.0]
    assert throttle.wait("other.test") == 0.0  # different domain is independent


def test_throttle_disabled_when_interval_zero():
    throttle = DomainThrottle(min_interval=0)
    assert throttle.wait("example.test") == 0.0
    assert throttle.wait("example.test") == 0.0


# --- providers -------------------------------------------------------------------


def test_mock_search_provider_respects_limit():
    provider = MockSearchProvider(
        [SearchHit(url=f"https://source{i}.example.test/item") for i in range(5)]
    )
    assert len(provider.search("Northstar", "focus", 2)) == 2


def test_real_provider_without_credentials_is_actionable():
    settings = SimpleNamespace(search_provider="tavily")
    with pytest.raises(SearchProviderUnavailable) as excinfo:
        get_search_provider(settings)
    message = str(excinfo.value)
    assert "SEARCH_PROVIDER=mock" in message  # tells the user how to run offline
    assert "scraping" not in message or "No fallback" in message


def test_mock_provider_selected_for_fixture_mode():
    settings = SimpleNamespace(search_provider="mock")
    assert isinstance(get_search_provider(settings), MockSearchProvider)


# --- mock fetcher determinism ----------------------------------------------------


def test_mock_fetcher_repeats_last_outcome():
    fetcher = MockFetcher(
        {
            "https://x.test": [  # first attempt fails, then always succeeds
                FetchOutcome(None, None, None, None, "boom"),
                FetchOutcome(200, "text/html", b"ok", "https://x.test"),
            ]
        }
    )
    assert fetcher.fetch("https://x.test", 1).status_code is None
    assert fetcher.fetch("https://x.test", 1).status_code == 200
    fetched = fetcher.fetch("https://x.test", 1)
    assert fetched.status_code == 200 and fetched.body == b"ok"
