# Source policy

## What PortfolioPulse may collect

Public, non-credentialed web pages reached by ordinary HTTP GET:

- Official company sources: corporate sites, investor-relations pages, press
  releases (`source_type=official_company_announcement` / `official_page`).
- News and trade press, and search-provider results (Gemini grounding), each
  recorded with publisher, retrieval timestamp, and the verbatim page text.

Every source lands in the database with its URL, canonical URL, content hash,
publisher, HTTP status, and retrieval time, including the ones that failed.

## Refusals are recorded, never bypassed

If a source refuses collection (HTTP 403/401, a login wall, a consent gate),
the source is recorded with status `blocked` and the run continues with the
remaining sources. The system never:

- retries against guessed mirror URLs,
- rotates user agents to defeat blocking,
- fabricates content for a source it could not read.

A blocked source is an honest gap, surfaced in the run's warnings and gap
report — not an obstacle to route around.

## robots.txt is honoured (fetch/parse/check, cached per origin)

Before any network fetch (real provider modes, not offline fixtures), the
collector checks robots.txt:

- `collect.py` builds a `RobotsGate`: it fetches and caches `robots.txt` per
  origin and asks the standard library parser whether `PortfolioPulse/0.1` may
  fetch the URL.
- A `Disallow` for our agent records the source as `blocked_by_robots` and no
  request is made. A refusal is a gap in the registry, never bypassed.
- An unreachable or malformed robots.txt does not block (standard practice);
  no permission is invented either way and the per-domain throttle still applies.

Still an open obligation (unchanged):

- ToS is not read anywhere. HTTP success is not permission to republish: keep
  exports internal unless source licences are checked.
- Per-source permission persistence (`allowed_by_robots`, checked-at date) is
  not yet recorded; the check happens live at collection time.

## Politeness mechanics that do exist

- Per-domain minimum interval between requests (`DomainThrottle`,
  `MIN_REQUEST_INTERVAL_SECONDS`, default 1 s).
- Bounded timeouts and response-size caps (`HTTP_TIMEOUT_SECONDS`,
  per-run source ceiling, `truncated_by_limit` recorded honestly).
- Strict dedupe by canonical URL + content hash — a page is fetched once per
  run, never repeatedly.
- Transient errors get a small bounded retry; permanent failures are recorded.
