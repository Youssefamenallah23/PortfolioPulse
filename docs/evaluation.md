# Evaluation

## What this evaluates

Evidence collection and reporting quality — **not** private company performance.
The harness (`src/portfoliopulse/evaluation.py`) runs the complete pipeline over
a fixture dataset, applies a documented review policy, exports the approved
packs, and measures the spec's metrics from persisted data.

## Dataset (10 companies, one industry)

`src/portfoliopulse/evaluation_fixtures.py` defines ten **explicitly fictional**
companies in industrial components (Aalborg Fasteners … Kaunas Electronics),
each with its own fixture pages on reserved `.test` domains: an official
acquisition announcement, a trade-press expansion note, and — for one company —
a competing analyst blog. All data is invented; nothing here is a real company,
acquisition, or figure.

Why fixtures: a real 10–15-company dataset requires live collection plus hours
of human annotation, and the collection layer currently has no robots.txt/ToS
honouring (`docs/source-policy.md`). The dataset description, annotation
schema, and procedure for the real dataset are below; the machinery is
provider-agnostic and runs unchanged on live runs.

**Built-in traps** (so the metrics have teeth, by construction):

| Trap | Company | Effect |
|---|---|---|
| "Partly supported": excerpt says *approximately 48/39 million*, extracted wording drops the qualifier | Carmona Pumps, Gdansk Valves | counts against precision |
| Prose date *"On 14 July 2026"* — the strict-ISO-date rule keeps `event_date` NULL | Jonkoping Sensors | counts as missing date |
| Claim whose supporting excerpt exists nowhere in the page | Ferrol Castings | flagged by validation, rejected in review; counts against citation relevance |
| Two pages report different purchase prices (60 vs 42) | Kaunas Electronics | gold: conflicting; tests the DISPUTED disclosure path |

## Gold labels

Each claim carries an annotation as a careful human would label it:
`supported` / `partly` / `unsupported` / `incorrect` / `conflicting`, plus the
date the source actually states (`gold_date`). For the fixture set these are
known by construction; for real runs the same fields arrive via an annotation
CSV (schema below).

## Review policy (the harness does not rubber-stamp)

`STRICT_POLICY`: system-supported claims are approved; validated-conflicting
claims are approved so the DISPUTED disclosure path is exercised; claims that
failed deterministic validation are rejected with a note. Every decision is a
real `Review` row — nothing bypasses the audit.

## Metric definitions

- **Claim support precision** — gold-supported among claims the *system* labels
  `supported` / all system-labelled `supported`. ("Partly" counts against.)
- **Citation relevance** — share of claims whose supporting excerpt occurs
  verbatim in the stored text of a cited source.
- **Unsupported-claim rate in approved outputs** — gold
  `unsupported`/`incorrect` claims among briefing-visible claims / all visible.
- **Missing/incorrect date rate** — gold-dated events where the system's
  `event_date` is wrong or absent.
- **Duplicate source rate / duplicate event rate** — duplicate canonical URLs;
  duplicate `(claim_text, event_date)` pairs.
- **End-to-end time** — automated wall time per run. Human verification time is
  **not** included (no human panel ran); reported separately below.
- **Hypothesis usefulness / cost per run** — human-only inputs. Reported as
  **not measured** unless provided; never estimated.

## Measured results (fixture dataset, strictly reviewed runs)

- Companies in dataset: **10**; sources collected: **21**; claims extracted: **33**
- Claim support precision: **0.933** (28/30) — the two "partly supported" traps
  are the misses; both drop the *approximately* qualifier (deterministic
  validation checks numbers, not qualifiers — an LLM-entailment layer is the
  planned fix, deliberately deferred)
- Citation relevance: **0.970** (32/33) — the single miss is the planted
  no-excerpt trap, which deterministic validation caught and strict review
  rejected before export
- Unsupported claims in approved briefing: **0** of 32 visible → rate **0.000**
- Gold-conflicting claims disclosed as DISPUTED: **2 of 2** (and the manifest
  carries an explicit conflicts warning)
- Missing/incorrect date rate: **1 of 10** gold-dated events → **0.100** — the
  prose-date case; strict "ISO or nothing" is a design tradeoff, disclosed here
- Duplicate source rate **0.000**, duplicate event rate **0.000**
- Hypotheses drafted: **9** (Kaunas correctly gets none: its only supported
  acquisition claim is 1 — below the ≥2 threshold)
- Avg automated end-to-end time: **≈0.08 s/run** (excludes human review; the
  demo-mode workbench adds human review time measured by the analyst)
- Human usefulness ratings: **not measured** (needs a human panel)
- Cost per run: **not measured in fixture mode** (fixture providers cost
  nothing); real Gemini runs should be annotated from the provider dashboard

Reproduce: `uv run python -m portfoliopulse.evaluation --report docs/evaluation-fixture-results.md`
(fully offline, ~5 s). Same numbers every run — the dataset is deterministic.

## Targets (spec) — never reported as achievements

| Metric | Target | Measured |
|---|---|---|
| Claim support precision | ≥ 0.9 | 0.933 ✅ |
| Citation relevance | high (all citations support) | 0.970 ✅ |
| Unsupported rate in approved outputs | ≈ 0 | 0.000 ✅ |
| Missing/incorrect date rate | low | 0.100 ⚠️ (prose-date tradeoff, disclosed) |
| Duplicate source/event rate | ≈ 0 | 0.000 ✅ |
| Human usefulness / cost | track | not measured |

## Annotation schema for real runs (CSV)

`claim_id,run_id,company,claim_text,excerpt,date,system_verification,label,gold_date,usefulness_1to5,cost_usd,notes`

- `label`: `supported | partly | unsupported | incorrect | conflicting`
- `gold_date`: ISO date the source states, else empty
- `usefulness_1to5`, `cost_usd`: optional human/provider inputs consumed by the
  report generator when present

## Procedure for the real 10–15-company dataset

1. Fix the source policy first (`docs/source-policy.md`): implement robots.txt
   honouring + per-source permission notes.
2. Pick one industry; run the server with `SEARCH_PROVIDER=gemini
   LLM_PROVIDER=gemini` over 10–15 public companies.
3. Export each approved run; annotate every claim with the CSV above
   (two passes recommended; record disagreements).
4. Feed annotations to the report generator; report precision/relevance/date
   metrics *per run and pooled*, plus median human minutes per run.

## Known evaluation limitations

- The fixture extractor is deterministic, so "LLM failure modes" are not
  sampled here — only the system's own rules (qualifier drops, prose dates,
  missing excerpts) are measured. Real-provider runs shift precision
  accordingly; the harness is unchanged.
- No human panel ran: usefulness ratings and human-minutes-per-run stay open.
- 10 fixture companies, not 10 real ones — dataset realism is the explicit gap.
