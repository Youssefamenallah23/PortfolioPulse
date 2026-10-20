# Data model

Eleven tables form one evidence chain: a statement is only as good as the
source, text, and review rows behind it.

```
Company ──< ResearchRun ──< Source ──1:1── SourceText
                 │              │
                 │              └────< ClaimSource >──── Claim
                 │                                        │
                 ├──< Hypothesis ──< HypothesisClaim >────┤
                 ├──< Review                              │
                 └──< ValidationIssue ────────────────────┘
```

## Entities

| Table | Role | Key fields |
|---|---|---|
| `Company` | research subject | `name`, optional `website`, `sector`, `country` |
| `ResearchRun` | one bounded research execution | `status` (queued/running/awaiting_review/approved/failed), `research_focus`, `config_summary` (provider names + limits, **no secrets**), `error_summary`, counters, `started_at`/`finished_at` |
| `Source` | one fetched URL per run | `url` + `canonical_url` (dedupe key), `content_hash`, `publisher`, `source_type`, `http_status`, `status` (fetched/duplicate/blocked/failed/unsupported_content_type/truncated), `extraction_status`, `retrieved_at` |
| `SourceText` | the verbatim fetched text (1:1 with Source) | `text`, `char_count` — the ground truth excerpts are checked against |
| `Claim` | a single atomic statement | `claim_text`, `category`, `supporting_excerpt`, `event_date` (ISO only, else NULL), `verification_status` (supported/unverified/conflicting), `review_status`, `extraction_status` |
| `ClaimSource` | provenance junction claim↔source | every claim cites ≥1 source; written via ORM relationship only |
| `Hypothesis` | a preliminary question to investigate | `hypothesis_text` (`DRAFT —` prefix until reviewed), `theme`, `title`, `alternatives`/`missing_evidence`/`validation_questions` (JSON), `evidence_summary` (JSON), `status` |
| `HypothesisClaim` | which supported claims feed a hypothesis | junction via ORM relationship |
| `Review` | immutable human decision rows | `target_type`+`target_id`, `decision` (approved/edited/rejected/flagged), `reviewer_notes`, `edited_text` |
| `ValidationIssue` | deterministic finding against one claim | `issue_type`, `message`, `review_status` (pending/resolved/waived) |

## Invariants enforced in code (not by hope)

- **No claim without provenance** — claims are created only with `ClaimSource`
  links, and validation flags anything whose source metadata is incomplete.
- **Excerpt must be literal** — an excerpt that doesn't occur in the linked
  `SourceText` becomes an `excerpt_not_found` issue; the claim is then
  `unverified` and cannot feed a hypothesis.
- **Dates: strict ISO or nothing** — `event_date` is stored only when the source
  states an unambiguous `YYYY-MM-DD`; prose dates stay NULL (measured honestly
  as the main date-error source in `docs/evaluation.md`).
- **Verification vs review are different axes** — `verification_status` is set
  automatically (`supported`/`unverified`/`conflicting`); `review_status`
  changes only through `record_review`. Only review can set `rejected`.
- **Idempotent ingestion** — `ingest_source` is a no-op for a known
  (`run_id`, `canonical_url`, `content_hash`); re-runs never duplicate sources.
- **Timestamps** are naive UTC inside the database (`utcnow()`); conversion to
  ISO+Z happens only at output edges (manifest, payloads).

## `Event` is not a table

A dated, material development is simply a `Claim` whose category is one of
`EVENT_CATEGORIES` (`acquisition`, `leadership`, `geographic_expansion`,
`strategy`) and whose `event_date` is non-null. The Excel *Events* sheet
(dated-only) and briefing section 4 derive from these. Undated developments may
appear in the briefing's developments section, but never with an invented date.

## Concurrency notes

In-memory SQLite uses SQLAlchemy `StaticPool` so every session shares one
connection (tests, evaluation harness). File-backed SQLite relies on WAL-free
single-process use — the API's BackgroundTasks executor uses its own session
per run; SQLite serializes the rest. No migrations exist yet: `make_engine`
runs `create_all`, which is sufficient while the schema is append-only.
