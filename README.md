# PortfolioPulse

An evidence-first research workbench for PE-backed company research. PortfolioPulse
collects public sources, extracts candidate claims with proof excerpts, validates
them deterministically, drafts hypotheses — and never lets anything reach
"approved" without persisted human review.

**Hard rules built into the code:**

- **Evidence-first** — every statement in an output cites a listed source plus a
  verbatim supporting excerpt.
- **Never invent data** — unknown stays `null`; a failed fetch is recorded, not
  retried against a guessed mirror.
- **Fact ≠ interpretation** — claims are observations; hypotheses are explicitly
  preliminary questions, never findings.
- **Deterministic-first** — LLMs are used only for extraction/search; all
  validation (excerpt presence, number overstatement, date sanity, conflict
  detection) is deterministic code.
- **Human gate** — exports are gated on run approval, and approval is gated on
  completed claim/hypothesis review. No review, no export. Ever.

## Quickstart (fully offline, 60 seconds)

```bash
uv sync                                  # install deps
uv run pytest -q                         # 156 tests should pass
uv run python -m portfoliopulse.evaluation   # run the 10-company evaluation
```

Then the offline demo (fictional fixture company, zero network, zero keys):

```bash
$env:SEARCH_PROVIDER='demo'; $env:LLM_PROVIDER='demo'   # PowerShell (demo mode)
uv run uvicorn portfoliopulse.main:app --port 8000
```

Open `http://127.0.0.1:8000/app`:

1. Type a company name + research focus → **Start research** (the demo serves a
   fictional "Northstar Components" fixture graph regardless of the name).
2. The run panel auto-refreshes to `awaiting_review` — review each claim
   (approve / edit / reject / flag) and the draft hypothesis.
3. **Approve for export** → **Generate exports** → download
   `analyst_brief.md` / `research_pack.xlsx` from the rendered links.

## Using the real provider (Gemini)

1. Open `http://127.0.0.1:8000/key`, paste your `GEMINI_API_KEY` (stored in
   `.env`; also settable directly). Check `http://127.0.0.1:8000/provider-status`.
2. Run the server with `SEARCH_PROVIDER=gemini LLM_PROVIDER=gemini`
   (`GEMINI_MODEL` defaults to `gemini-3.5-flash-lite`).
3. Start a run from the workbench as above. Costs are visible in your Google AI
   dashboard; PortfolioPulse itself never logs secrets (see `config_summary`,
   which records provider names only).

## API surface (Step 10)

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

`/health` and the review conveniences from Step 8 (`GET /runs`,
`GET /runs/{id}/review`, `POST /reviews`) remain available.

## Repository map

```
src/portfoliopulse/
  config.py        settings (env/.env; secrets never persisted or logged)
  db.py            engine/session (SQLite; StaticPool for in-memory)
  models.py        the evidence-chain schema (see docs/data-model.md)
  collect.py       fetching, search, dedupe, throttling (provider-agnostic)
  extract.py       untrusted LLM output → strict schema → claims (+ repair retry)
  validate.py      deterministic validation issues
  hypotheses.py    evidence-linked draft hypotheses
  review.py        human review service (approve/edit/reject/flag; audit rows)
  pipeline.py      explicit typed state machine over all of the above
  exports.py       gated Markdown + Excel exports (run-scoped, manifest)
  demo.py          offline demo fixtures (fictional, `.test` domains)
  evaluation_fixtures.py / evaluation.py   Step 11 metric harness
  main.py          FastAPI app + minimal workbench UI
docs/              architecture, data model, evaluation, source policy, examples
tests/             156 tests, fully offline
```

## Documentation

- `docs/architecture.md` — how the pieces fit, why plain orchestration
- `docs/data-model.md` — entities and the evidence chain
- `docs/evaluation.md` — dataset, metric definitions, measured results, targets
- `docs/source-policy.md` — permitted sources, refusals, known limitations
- `docs/examples/` — one generated fixture export set (brief, workbook, manifest)

## Scope and limitations (honest)

- SQLite by design for the MVP; BackgroundTasks (not a durable queue) execute
  runs — a restart loses a `queued` run.
- No robots.txt/ToS honouring layer: a refused source (e.g. HTTP 403) is recorded
  as `blocked` and never bypassed — see `docs/source-policy.md`.
- The evaluation dataset is fictional fixtures; a real 10–15-company dataset
  requires the source-policy work plus human annotation (procedure documented).
