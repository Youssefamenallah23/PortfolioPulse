"""Source collection: URL normalization, fetching, politeness, and the source registry.

Source-access policy (hard rules):
- Access refusals (401/403/407/429) are RECORDED as `blocked`, never retried with
  workarounds, proxies, or header spoofing.
- No search-engine result-page scraping: search goes through a SearchProvider
  (fixture/mock now; real providers configurable via env when implemented).
- Every fetch has a timeout and bounded retries; per-domain throttling spaces requests.

Statuses written to models.Source.status: fetched | failed | blocked |
blocked_by_robots | duplicate | unsupported_content_type.
"Unknown" stays None (never faked).
"""

from __future__ import annotations

import hashlib
import html as html_lib
import re
import time
import urllib.request as urllib_request
import urllib.robotparser as urllib_robotparser
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import httpx
from sqlalchemy.orm import Session

from portfoliopulse import models as m

STATUS_FETCHED = "fetched"
STATUS_FAILED = "failed"  # inaccessible: 404, 410, 5xx, timeout, connection errors
STATUS_BLOCKED = "blocked"  # access refusal: 401/403/407/429
STATUS_BLOCKED_ROBOTS = "blocked_by_robots"  # robots.txt disallows this path for us
STATUS_DUPLICATE = "duplicate"
STATUS_UNSUPPORTED = "unsupported_content_type"  # unknown type or oversized body

TRACKING_PARAMS = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "fbclid",
    "gclid",
    "mc_cid",
    "mc_eid",
    "ref",
    "referrer",
    "igshid",
}
DEFAULT_PORTS = {"http": "80", "https": "443"}

# What a fetcher is allowed to analyse. Unknown types are recorded, not guessed at.
ALLOWED_CONTENT_TYPES = (
    "text/html",
    "application/xhtml+xml",
    "text/plain",
    "application/rss+xml",
    "application/xml",
    "text/xml",
    "application/json",
    "application/pdf",
)

USER_AGENT = "PortfolioPulse/0.1 (research fixture; honours refusals and rate limits)"

MAX_BODY_BYTES = 2_000_000  # generous default; per-call override for tests


# ---------------------------------------------------------------------------
# URL normalization + publisher extraction (deterministic, no LLM)
# ---------------------------------------------------------------------------


def canonical_url(url: str) -> str:
    """Deterministic canonical form: lowercase host, no fragment, no default port,
    tracking parameters stripped, remaining query parameters sorted."""
    p = urlparse(url.strip())
    if p.scheme not in ("http", "https") or not p.netloc:
        raise ValueError(f"not an absolute http(s) URL: {url!r}")
    host = p.netloc.lower()
    scheme = p.scheme.lower()
    if ":" in host and not host.startswith("["):
        hostname, _, port = host.partition(":")
        if port == DEFAULT_PORTS.get(scheme):
            host = hostname
    params = sorted(
        (k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if k not in TRACKING_PARAMS
    )
    return urlunparse((scheme, host, p.path or "/", "", urlencode(params), ""))


def publisher_of(url: str) -> str:
    """example.com from https://www.Example.com/path (www stripped, host lowercased)."""
    host = (urlparse(url).hostname or "").lower()
    return host.removeprefix("www.")


# ---------------------------------------------------------------------------
# Fetch outcomes, protocols, and implementations
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FetchOutcome:
    status_code: int | None
    content_type: str | None
    body: bytes | None
    final_url: str | None
    error: str | None = None


class PageFetcher(Protocol):
    def fetch(self, url: str, timeout: float) -> FetchOutcome: ...


@dataclass(frozen=True)
class SearchHit:
    url: str
    title: str | None = None
    source_type: str | None = None


class SearchProvider(Protocol):
    def search(self, company: str, focus: str, limit: int) -> list[SearchHit]: ...


def _content_type_of(header: str | None) -> str | None:
    if not header:
        return None
    return header.split(";", 1)[0].strip().lower() or None


def _is_transient(outcome_like: FetchOutcome) -> bool:
    """5xx answers are retryable; 4xx are truthful refusals, not transient."""
    code = outcome_like.status_code
    return code is not None and 500 <= code < 600


class HttpxPageFetcher:
    """Real fetch path: bounded retries with exponential backoff on transient
    failures only. Refusals (4xx) and permanent errors pass through immediately."""

    def __init__(
        self,
        client: httpx.Client,
        attempts: int = 3,
        backoff_base: float = 0.5,
        sleeper: Callable[[float], None] = time.sleep,
    ):
        if attempts < 1:
            raise ValueError("attempts must be >= 1")
        self.client = client
        self.attempts = attempts
        self.backoff_base = backoff_base
        self.sleeper = sleeper

    def fetch(self, url: str, timeout: float) -> FetchOutcome:
        last_error: str | None = None
        for attempt in range(self.attempts):
            try:
                response = self.client.get(
                    url,
                    timeout=timeout,
                    follow_redirects=True,
                    headers={"User-Agent": USER_AGENT},
                )
                ctype = _content_type_of(response.headers.get("content-type"))
                if 200 <= response.status_code < 300:
                    return FetchOutcome(
                        response.status_code, ctype, response.content, str(response.url)
                    )
                outcome = FetchOutcome(response.status_code, ctype, None, str(response.url))
                if not _is_transient(outcome) or attempt == self.attempts - 1:
                    return outcome  # truthful refusal or retries exhausted
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if isinstance(exc, httpx.TooManyRedirects):
                    return FetchOutcome(None, None, None, None, error=f"too_many_redirects: {exc}")
                last_error = f"{type(exc).__name__}: {exc}"
            if attempt < self.attempts - 1:
                self.sleeper(self.backoff_base * (2**attempt))
        return FetchOutcome(None, None, None, None, error=last_error)


class MockFetcher:
    """Deterministic test double: url -> sequence of outcomes (last repeats).
    Keys are canonicalised so tracking parameters in the requested URL never
    change which fixture matches. Never touches the network."""

    def __init__(self, outcomes: dict[str, Sequence[FetchOutcome]]):
        self.outcomes = {canonical_url(k): list(v) for k, v in outcomes.items()}

    def fetch(self, url: str, timeout: float) -> FetchOutcome:
        try:
            key = canonical_url(url)
        except ValueError:
            key = url
        seq = self.outcomes.get(key)
        if not seq:
            return FetchOutcome(None, None, None, None, error=f"no fixture for {url!r}")
        item = seq[0]
        remaining = seq[1:]
        if remaining:  # next attempt gets the following outcome
            self.outcomes[key] = remaining
        else:  # single outcome repeats forever
            self.outcomes[key] = [item]
        return item


class MockSearchProvider:
    """Deterministic test/fixture search provider; empty demo provider by default."""

    provider_name = "mock"

    def __init__(self, hits: Sequence[SearchHit] = ()):
        self.hits = list(hits)

    def search(self, company: str, focus: str, limit: int) -> list[SearchHit]:
        del company, focus
        return self.hits[:limit]


class SearchProviderUnavailable(RuntimeError):
    """Actionable configuration error — the app must fall back to fixture mode,
    never silently scrape something else instead."""


def get_search_provider(settings: Any) -> SearchProvider:
    name = getattr(settings, "search_provider", "mock")
    if name == "mock":
        return MockSearchProvider()
    if name == "demo":
        from portfoliopulse.demo import DemoSearchProvider

        return DemoSearchProvider()
    if name == "gemini":
        from portfoliopulse.gemini import GeminiSearchProvider

        return GeminiSearchProvider(
            api_key=getattr(settings, "gemini_api_key", ""),
            model=getattr(settings, "gemini_model", "gemini-3.5-flash-lite"),
        )
    raise SearchProviderUnavailable(
        f"Search provider {name!r} is not configured. Set SEARCH_PROVIDER=mock "
        "(offline empty mode), demo (offline fictional fixture demo), or gemini "
        "(real provider, see .env.example). No fallback scraping is performed."
    )


def get_fetcher(settings: Any) -> tuple[PageFetcher, Callable[[], None]]:
    """Page fetcher matched to the selected mode. Returns (fetcher, close).

    "demo" mode (and mock, which never plans hits anyway) stays fully offline;
    every other mode fetches real pages via httpx with bounded retries.
    """
    name = getattr(settings, "search_provider", "mock")
    if name in ("demo", "mock"):
        if name == "demo":
            from portfoliopulse.demo import demo_fetcher

            return demo_fetcher(), (lambda: None)
        return MockFetcher({}), (lambda: None)
    import httpx

    client = httpx.Client(follow_redirects=True)
    return HttpxPageFetcher(client), client.close


# ---------------------------------------------------------------------------
# Politeness: per-domain throttle
# ---------------------------------------------------------------------------


class DomainThrottle:
    """Enforces a minimum interval between requests to the same domain."""

    def __init__(
        self,
        min_interval: float,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ):
        self.min_interval = min_interval
        self.clock = clock
        self.sleeper = sleeper
        self._last: dict[str, float] = {}

    def wait(self, domain: str) -> float:
        if self.min_interval <= 0:
            return 0.0
        now = self.clock()
        elapsed = now - self._last.get(domain, now - (self.min_interval + 1))
        remaining = self.min_interval - elapsed
        if remaining > 0:
            self.sleeper(remaining)
            now = self.clock()
        self._last[domain] = now
        return max(0.0, remaining)


# ---------------------------------------------------------------------------
# Collection pipeline: fetch -> classify -> persist (idempotent)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UrlTarget:
    url: str
    title: str | None = None
    source_type: str = "web_page"


@dataclass
class CollectReport:
    fetched: int = 0
    duplicates: int = 0
    blocked: int = 0
    failed: int = 0
    unsupported: int = 0
    blocked_by_robots: int = 0
    truncated_by_limit: int = 0

    def totals(self) -> dict[str, int]:
        return {
            "fetched": self.fetched,
            "duplicates": self.duplicates,
            "blocked": self.blocked,
            "failed": self.failed,
            "unsupported": self.unsupported,
            "blocked_by_robots": self.blocked_by_robots,
            "truncated_by_limit": self.truncated_by_limit,
        }


# Main-content extraction: trafilatura parses document structure (drops nav, cookie
# banners and boilerplate, reads meta dates). When it yields nothing (bare fixture
# pages, plain-text bodies) the old naive flattener keeps the exact previous behavior.
import trafilatura as _trafilatura

_SCRIPT_RE = re.compile(r"<(script|style)\b.*?</\1>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")


def _naive_text_from_html(body: bytes) -> str:
    text = body.decode("utf-8", errors="replace")
    text = _SCRIPT_RE.sub(" ", text)
    text = _TAG_RE.sub(" ", text)
    text = html_lib.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _parse_meta_date(value: str | None):
    """Naive-UTC datetime from a metadata date string; garbage stays None."""
    if not value:
        return None
    text_val = value.rstrip()[:-1] + "+00:00" if value.rstrip().endswith("Z") else value.rstrip()
    try:
        parsed = datetime.fromisoformat(text_val)
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.UTC).replace(tzinfo=None)
    return parsed


def _text_from_html(body: bytes) -> tuple[str, str | None]:
    """Page text plus, when the page metadata declares it, its publication date string."""
    html_text = body.decode("utf-8", errors="replace")
    try:
        extracted = _trafilatura.extract(html_text, include_comments=False)
    except Exception:  # noqa: BLE001 - a parser crash must not fail the collect node
        extracted = None
    if not extracted or not extracted.strip():
        return _naive_text_from_html(body), None
    published = None
    try:
        meta = _trafilatura.extract_metadata(html_text)
    except Exception:  # noqa: BLE001 - metadata is best-effort
        meta = None
    published = getattr(meta, "date", None) if meta is not None else None
    return re.sub(r"\s+", " ", extracted).strip(), published


class RobotsGate:
    """Honours robots.txt per origin with the standard library parser.

    An unreachable or malformed robots.txt never blocks (standard practice);
    an explicit Disallow for our user agent records the source as
    blocked_by_robots and no fetch is attempted. One fetch per origin, cached.
    """

    def __init__(self, fetch_robots: Callable[[str], str | None] | None = None):
        self._cache: dict[str, urllib_robotparser.RobotFileParser | None] = {}
        self._fetch: Callable[[str], str | None] = fetch_robots or self._read_robots

    @staticmethod
    def _read_robots(origin: str) -> str | None:
        try:
            with urllib_request.urlopen(f"{origin}/robots.txt", timeout=8) as resp:
                if getattr(resp, "status", 200) == 200:
                    return resp.read().decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001 - unreachable robots.txt must never block
            return None
        return None

    def allowed(self, url: str, user_agent: str = USER_AGENT) -> bool:
        parts = urlparse(url)
        if parts.scheme not in ("http", "https"):
            return True
        origin = f"{parts.scheme}://{parts.netloc.lower()}"
        if origin not in self._cache:
            text = self._fetch(origin)
            parser = None
            if text is not None:
                parser = urllib_robotparser.RobotFileParser()
                parser.parse(text.splitlines())
            self._cache[origin] = parser
        parser = self._cache[origin]
        return parser is None or parser.can_fetch(user_agent, url)


def collect_url(
    session: Session,
    run: m.ResearchRun,
    company: m.Company,
    target: UrlTarget,
    *,
    fetcher: PageFetcher,
    timeout: float = 15.0,
    throttle: DomainThrottle | None = None,
    max_bytes: int = MAX_BODY_BYTES,
    robots: RobotsGate | None = None,
) -> tuple[m.Source, str]:
    """Fetch one URL and record the outcome honestly. Returns (source_row, status)."""
    malformed = False
    try:
        canonical = canonical_url(target.url)
    except ValueError:
        canonical = target.url
        malformed = True

    if throttle is not None:
        throttle.wait(urlparse(canonical).hostname or canonical)

    if malformed:
        source, _ = m.ingest_source(
            session,
            run.id,
            company.id,
            url=target.url,
            canonical_url=canonical,
            title=target.title,
            source_type=target.source_type,
            status=STATUS_FAILED,
        )
        return source, STATUS_FAILED

    # robots.txt is checked before any pull; a refusal is recorded, never bypassed
    if robots is not None and not robots.allowed(canonical):
        source, _ = m.ingest_source(
            session,
            run.id,
            company.id,
            url=target.url,
            canonical_url=canonical,
            title=target.title,
            source_type=target.source_type,
            status=STATUS_BLOCKED_ROBOTS,
            publisher=publisher_of(canonical),
        )
        return source, STATUS_BLOCKED_ROBOTS

    outcome = fetcher.fetch(target.url, timeout)

    # Classify the outcome.
    if outcome.status_code in (401, 403, 407, 429):
        status = STATUS_BLOCKED
    elif outcome.status_code is None or outcome.status_code >= 400 or outcome.body is None:
        status = STATUS_FAILED
    elif outcome.content_type not in ALLOWED_CONTENT_TYPES or len(outcome.body) > max_bytes:
        status = STATUS_UNSUPPORTED
    else:
        status = STATUS_FETCHED

    fields = {
        "title": target.title or None,
        "source_type": target.source_type,
        "http_status": outcome.status_code,
        "publisher": publisher_of(canonical),
    }

    if outcome.body is not None:
        fields["content_hash"] = "sha256:" + hashlib.sha256(outcome.body).hexdigest()

    source, created = m.ingest_source(
        session,
        run.id,
        company.id,
        url=target.url,
        canonical_url=canonical,
        status=status,
        **fields,
    )

    if not created:
        if source.status == STATUS_FETCHED:
            # Already have this (or identical) content recorded — no duplicate row.
            return source, STATUS_DUPLICATE
        if status == STATUS_FETCHED:
            # A previously failed URL now works: refresh the SAME row (idempotent).
            pass
        else:
            source.status = status  # refresh a failed record with the new outcome
            source.http_status = outcome.status_code
            session.flush()
            return source, status

    if status != STATUS_FETCHED:
        return source, status

    text, meta_date = _text_from_html(outcome.body)  # type: ignore[arg-type]
    source.excerpt = text[:280] if text else None
    source.status = STATUS_FETCHED
    published = _parse_meta_date(meta_date)
    if published is not None:
        source.published_at = published
    existing_text = session.get(m.SourceText, source.id)
    if existing_text:
        existing_text.text = text
    else:
        session.add(m.SourceText(source_id=source.id, text=text))
    run.source_count = session.query(m.Source).filter_by(run_id=run.id).count()
    session.flush()
    return source, STATUS_FETCHED


def collect_targets(
    session: Session,
    run: m.ResearchRun,
    company: m.Company,
    targets: Iterable[UrlTarget],
    *,
    fetcher: PageFetcher,
    timeout: float = 15.0,
    throttle: DomainThrottle | None = None,
    max_sources_per_run: int = 20,
    max_bytes: int = MAX_BODY_BYTES,
    robots: RobotsGate | None = None,
) -> CollectReport:
    """Collect a batch of targets while enforcing the per-run source ceiling."""
    report = CollectReport()

    def source_count() -> int:
        return session.query(m.Source).filter_by(run_id=run.id).count()

    for target in targets:
        if max_sources_per_run - source_count() <= 0:
            report.truncated_by_limit += 1
            continue
        source, status = collect_url(
            session,
            run,
            company,
            target,
            fetcher=fetcher,
            timeout=timeout,
            throttle=throttle,
            max_bytes=max_bytes,
            robots=robots,
        )
        if status == STATUS_FETCHED:
            report.fetched += 1
        elif status == STATUS_DUPLICATE:
            report.duplicates += 1
        elif status == STATUS_BLOCKED:
            report.blocked += 1
        elif status == STATUS_BLOCKED_ROBOTS:
            report.blocked_by_robots += 1
        elif status == STATUS_UNSUPPORTED:
            report.unsupported += 1
        else:
            report.failed += 1
        del source

    run.source_count = source_count()
    session.flush()
    return report
