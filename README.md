# PortfolioPulse

A research workbench for private equity analysts. You give it a company and a
research focus. It reads public pages, pulls out checkable statements, and puts
each one in front of you with its source and its proof quote. Nothing leaves the
system as approved until you have personally reviewed every item.

Three rules run through all of it:

- Every claim carries provenance: the source it came from and a verbatim excerpt
  from that source. If the excerpt is not literally in the stored page text,
  validation flags it.
- The system never invents data. A page that refuses to load is recorded as
  blocked, never retried against a guessed mirror. A missing value stays null.
  A disputed number stays disputed instead of being silently resolved.
- Facts and interpretations live apart. Claims are observations. Hypotheses are
  preliminary drafts, and they only exist when at least two supported claims
  back them.

## The workflow

Every run moves through the same stations, in order:

1. Scope. Register a company and write the research focus.
2. Collect. A search provider proposes pages; the fetcher stores each page's
   text with publisher and retrieval time. Duplicates collapse to one canonical
   URL. Refused pages (HTTP 403) are recorded as blocked, not bypassed.
3. Extract. An LLM reads each stored page and proposes claims, each with a
   verbatim supporting excerpt. LLM output is treated as untrusted input: it
   must parse against a strict schema, with one repair retry before the page is
   recorded as an extraction failure.
4. Validate. Deterministic code only, no model here: the excerpt must appear in
   the stored text, the claim may not contain figures its evidence lacks, event
   dates must make sense, and same-topic claims that disagree are both marked
   conflicting. No winner is picked; the analyst decides.
5. Hypothesize. When at least two supported claims share a theme, a draft
   hypothesis is written with alternative explanations, missing evidence, and
   concrete validation questions. Thin evidence produces no hypothesis at all.
6. Review. The human gate. Each claim can be approved, corrected, rejected, or
   flagged; each open validation issue must be resolved or waived. Rejected
   items stay in the audit trail but are excluded from exports.
7. Approve. Only possible when nothing is pending. The gate refuses with a
   reason otherwise.
8. Export. A Markdown briefing and an Excel pack are written, each with a
   checksum manifest, for approved runs only. Disputed claims are labelled
   `DISPUTED` in the briefing.

## Orchestration

The pipeline is a typed state machine in `src/portfoliopulse/pipeline.py`.
Plain Python, no workflow framework. Each station is a function over a typed
state object, the run row records the stage, a provider failure or crash marks
the run `failed`, and follow-up research rounds are bounded (2 by default) so a
search-then-read loop cannot run away.

If you know LangGraph, the correspondence is direct:

| PortfolioPulse | LangGraph concept |
|---|---|
| typed state dataclass | `StateGraph` state schema |
| stage functions (collect, extract, ...) | nodes |
| bounded follow-up round | conditional edge |
| run row status (`queued` / `running` / ...) | state persistence |
| human review gate | interrupt, human in the loop |

It is deliberately not built on LangGraph or LangChain. The graph is a straight
line with one loop; the whole behavior is a couple hundred readable, tested
lines. LangChain-style provider abstraction exists anyway: `collect.py` and
`extract.py` define a protocol plus a factory per provider (mock, demo, Gemini
adapters in `gemini.py`), so a LangGraph `StateGraph` could wrap the same stage
functions later without rewriting them.

## Try the offline demo (no keys, no network)

```powershell
uv sync                                    # install dependencies
uv run pytest -q                           # 156 tests should pass
$env:SEARCH_PROVIDER='demo'; $env:LLM_PROVIDER='demo'
uv run uvicorn portfoliopulse.main:app --port 8000
```

Open `http://127.0.0.1:8000/` (or `/app`). Type any company name and a research
focus, then Start research. The demo serves a fictional "Northstar Components"
fixture set regardless of the name.

What one demo run comes back with:

- 4 sources and 7 claims, all on reserved `.test` domains with invented figures.
- Two claims dispute the acquisition price: 48 million euros in the official
  announcement, around 60 million in trade press. Both enter the briefing as
  `DISPUTED`, and both feed one validation issue you must resolve or waive
  before the approve button works.
- 2 draft hypotheses (one on expansion coordination, one on operating-model
  strain), each with alternatives and missing evidence listed.

Then review each claim, resolve the price conflict, approve the run, generate
exports, and download `analyst_brief.md` / `research_pack.xlsx`. The pipeline
diagram at the top of the page shows the live counts at every station.

## Using the real provider (Gemini)

1. Open `http://127.0.0.1:8000/key`, paste your `GEMINI_API_KEY` (stored in
   `.env`; also settable directly). Check `http://127.0.0.1:8000/provider-status`.
2. Run the server with `SEARCH_PROVIDER=gemini LLM_PROVIDER=gemini`
   (`GEMINI_MODEL` defaults to `gemini-3.5-flash-lite`).
3. Start a run from the workbench as above. Expect a run to take a couple of
   minutes with real page fetches, and expect some pages to come back blocked;
   that is recorded, not hidden. Costs are visible in your Google AI dashboard;
   PortfolioPulse never logs secrets (`config_summary` records provider names
   only).

## API surface

| Method | Path | Purpose |
|---|---|---|
| POST | `/companies` | register a research subject |
| GET | `/companies`, `/companies/{id}` | list / inspect |
| POST | `/research-runs` | start a run; returns `run_id` + `queued`, executes in background |
| GET | `/research-runs/{id}` (+ `/sources`, `/claims`, `/hypotheses`) | progress + evidence |
| POST | `/claims/{id}/review`, `/hypotheses/{id}/review` | human review (approve/edit/reject/flag) |
| POST | `/validation-issues/{id}/status` | resolve / waive a validation issue |
| POST | `/research-runs/{id}/approve` | approval gate (400 with reason when blocked) |
| POST | `/research-runs/{id}/export` | generate Markdown + Excel (approved runs only) |
| GET | `/research-runs/{id}/exports` (+ `/exports/{file}`) | manifest + safe downloads |

`/health` and the review conveniences from the review step (`GET /runs`,
`GET /runs/{id}/review`, `POST /reviews`) remain available.

## Repository map

```
src/portfoliopulse/
  config.py        settings (env/.env; secrets never persisted or logged)
  db.py            engine/session (SQLite; StaticPool for in-memory)
  models.py        the evidence-chain schema (see docs/data-model.md)
  collect.py       fetching, search, dedupe, throttling (provider-agnostic)
  extract.py       untrusted LLM output to strict schema to claims (+ repair retry)
  validate.py      deterministic validation issues
  hypotheses.py    evidence-linked draft hypotheses
  review.py        human review service (approve/edit/reject/flag; audit rows)
  pipeline.py      explicit typed state machine over all of the above
  gemini.py        real provider adapters (extraction in JSON mode, grounding search)
  exports.py       gated Markdown + Excel exports (run-scoped, manifest)
  demo.py          offline demo fixtures (fictional, including a price conflict)
  workbench.py     the single-page analyst UI (live pipeline diagram, review desk)
  evaluation_fixtures.py / evaluation.py   metric harness
  main.py          FastAPI app wiring it together
docs/              architecture, data model, evaluation, source policy, examples
tests/             156 tests, fully offline
```

## Documentation

- `docs/architecture.md`: how the pieces fit, why plain orchestration
- `docs/data-model.md`: entities and the evidence chain
- `docs/evaluation.md`: dataset, metric definitions, measured results, targets
- `docs/source-policy.md`: permitted sources, refusals, known limitations
- `docs/examples/`: one generated fixture export set (brief, workbook, manifest)

## Scope and limitations (honest)

- SQLite by design for the MVP; BackgroundTasks (not a durable queue) execute
  runs, so a restart can lose a `queued` run. `POST /research-runs/{id}/resume`
  re-enters the idempotent pipeline for `queued` or `failed` runs.
- robots.txt is honoured on real fetches (`blocked_by_robots` recorded, no
  bypass); site Terms of Service are not read anywhere (see `docs/source-policy.md`).
- The evaluation dataset is fictional fixtures; a real 10 to 15 company dataset
  requires the source-policy work plus human annotation (procedure documented).
