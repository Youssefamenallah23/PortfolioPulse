# Architecture

## Design stance

PortfolioPulse is deliberately conservative. The problem is evidence discipline,
not orchestration exotics, so the stack stays small: Python 3.12+, FastAPI,
Pydantic v2, SQLAlchemy 2.x on SQLite, pytest, ruff. No vector store, no agent
fleet, no message queue.

> ponytail note: the orchestrator is a plain typed state machine, not LangGraph —
> the same explicit-transition contract with zero new dependencies. Swapping in
> LangGraph later is a mechanical adapter over the same node methods; do it when
> checkpointing/interrupts are actually needed.

## The pipeline

`pipeline.py` implements `ResearchPipeline` as an explicit, strictly sequential
state machine over typed nodes:

```
validate_request → plan_research → collect → extract → validate
      → (bounded follow-up collection rounds, ≤ max_research_rounds)
      → generate_hypotheses → review_pending → finalize
```

Properties that matter:

- **Every node persists its work** (`session.flush`), so a crash between stages
  leaves the run and its partial evidence intact rather than erasing it.
- **Every node is idempotent**: source dedupe by canonical URL + content hash,
  extraction skips already-extracted sources, validation recomputes only
  unvalidated claims, hypothesis generation skips runs that already have drafts.
- **Evidence gaps trigger at most `max_research_rounds` bounded collection
  rounds**, then the run ends honestly with a gap report in its warnings — never
  an infinite loop, never fabricated filler.
- **Approval is a separate gate** (`approve_run`): no pending claims, no draft
  hypotheses, no pending validation issues, run not failed. Failed runs cannot
  be approved.

## Provider-agnostic seams

Every external dependency sits behind a small protocol with a mock and a real
implementation, so the entire pipeline runs offline and deterministically:

| Seam | Protocol | Offline | Real |
|---|---|---|---|
| Page fetching | `PageFetcher` | `MockFetcher` / demo fixtures | `HttpxPageFetcher` |
| Search | `SearchProvider` | `MockSearchProvider` / demo | `GeminiSearchProvider` (grounding) |
| Extraction | `ClaimExtractor` | `MockClaimExtractor` / demo | `GeminiExtractor` (JSON mode) |

`demo.py` is the third mode: a fictional fixture company (reserved `.test`
domains) that drives the complete workbench flow — start → review → approve →
export — with zero network and zero keys.

## Untrusted LLM output

Extraction is a boundary, not a source of truth (`extract.py`):

- Model output must satisfy a strict Pydantic schema (`extra="forbid"`); on
  failure the model gets exactly one repair retry carrying the schema error;
  after that the source is marked `failed_extraction` — nothing is coerced.
- Page content is data, never instructions: the prompt wraps page text in
  untrusted-content markers, and the mock extractor demonstrates that embedded
  page instructions are ignored.
- Excerpts are only accepted if they literally occur in the stored source text.

## Validation (deterministic only)

`validate.py` records persisted `ValidationIssue` rows, each tied to a claim:
missing source metadata, stored text missing, excerpt not found, claim
overstating its excerpt (numbers not present in the evidence), date confusion,
conflicting claim pairs. Verification statuses `supported` / `unverified` /
`conflicting` are set automatically; `rejected` and review outcomes come only
from human review.

## Review, approval, export

- `review.py` records `Review` rows for every human decision
  (approve/edit/reject/flag on claims and hypotheses; resolve/waive on issues).
  Rejected claims disappear from outputs but stay in the audit trail.
- `exports.py` refuses anything but `approved` runs (`ExportBlocked`) and writes
  a Markdown briefing (10 fixed sections, footnote citations, DISPUTED labels)
  plus an Excel workbook (6 sheets) into a run-scoped directory with a
  `manifest.json`. Per-file failures are isolated into manifest warnings.

## API + workbench

`main.py` is a thin, stateless HTTP surface over the services. Run execution
uses FastAPI BackgroundTasks (documented MVP choice; a durable queue is the
first upgrade if restart-safety matters). The single-page workbench at `/`
covers exactly the key loop: start run → review → approve → export.

## What is intentionally absent

- Vector search / embeddings (SQLite + excerpt matching is enough at MVP scale)
- Retry-until-success scraping (refusals are recorded, never bypassed)
- Multi-agent "fleets" (one explicit state machine already gives the
  auditability the agents would fake)
- robots.txt/ToS enforcement (recorded limitation — see `docs/source-policy.md`)
