# Source policy

## What PortfolioPulse may collect

Public, non-credentialed web pages reached by ordinary HTTP GET:

- Official company sources: corporate sites, investor-relations pages, press
  releases (`source_type=official_company_announcement` / `official_page`).
- News and trade press, and search-provider results (Gemini grounding), each
  recorded with publisher, retrieval timestamp, and the verbatim page text.

Every source lands in the database with its URL, canonical URL, content hash,
publisher, HTTP status, and retrieval time — including the ones that failed.

## Refusals are recorded, never bypassed

If a source refuses collection (HTTP 403/401, a login wall, a consent gate),
the source is recorded with status `blocked` and the run continues with the
remaining sources. The system never:

- retries against guessed mirror URLs,
- rotates user agents to defeat blocking,
- fabricates content for a source it could not read.

A blocked source is an honest gap, surfaced in the run's warnings and gap
report — not an obstacle to route around.

## ⚠️ Known limitation: no robots.txt / ToS honouring layer (unguarded)

**The current MVP does not implement a robots.txt fetch/parse/honour layer, and
does not read site Terms of Service.** This is a deliberate scope cut for the
MVP and must be treated as an open obligation:

- Before any use beyond personal evaluation, implement: fetch and cache
  `robots.txt` per host, honour `Disallow`/`Crawl-delay`, and keep a per-source
  permission note (`allowed_by_robots`, checked date).
- Do not treat HTTP success as permission: a page that *can* be fetched is not
  necessarily a page that *may* be republished; exports should stay internal
  unless source licences are checked.
- Until that layer exists, users must ensure their own collection complies with
  applicable robots directives and site terms. The workbench's polite defaults
  (per-domain throttle, bounded timeouts, no parallel hammering) are politeness,
  not permission.

## Politeness mechanics that do exist

- Per-domain minimum interval between requests (`DomainThrottle`,
  `MIN_REQUEST_INTERVAL_SECONDS`, default 1 s).
- Bounded timeouts and response-size caps (`HTTP_TIMEOUT_SECONDS`,
  per-run source ceiling, `truncated_by_limit` recorded honestly).
- Strict dedupe by canonical URL + content hash — a page is fetched once per
  run, never repeatedly.
- Transient errors get a small bounded retry; permanent failures are recorded.
