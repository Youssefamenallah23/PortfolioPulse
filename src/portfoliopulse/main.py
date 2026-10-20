"""Application entry point.

Importing this module must not start research jobs or make network calls.
"""

import json
import logging
import threading
from typing import Annotated, Literal

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session as DBSessionBase

from portfoliopulse import models as m
from portfoliopulse.collect import (
    DomainThrottle,
    RobotsGate,
    SearchProviderUnavailable,
    get_fetcher,
    get_search_provider,
)
from portfoliopulse.config import effective_env, get_settings, update_env_var
from portfoliopulse.db import make_engine, make_session_factory, utcnow
from portfoliopulse.exports import (
    ExportBlocked,
    ExportWriteError,
    export_run,
    run_export_dir,
)
from portfoliopulse.extract import LLMProviderUnavailable, get_llm_provider
from portfoliopulse.logging_setup import setup_logging
from portfoliopulse.pipeline import ApprovalBlocked, ResearchPipeline, approve_run
from portfoliopulse.review import (
    ReviewInputError,
    ReviewNotFoundError,
    resolve_issue,
    review_claim,
    review_hypothesis,
    unresolved_summary,
)
from portfoliopulse.workbench import _APP_PAGE

logger = logging.getLogger("portfoliopulse.api")

settings = get_settings()
setup_logging()

app = FastAPI(title="PortfolioPulse", docs_url=None, redoc_url=None)

# Lazily created session factory so tests can freely override get_db.
# A lock guards the cold-start: concurrent first requests must not both create_all.
_session_factory = None
_session_factory_lock = threading.Lock()


def _get_session_factory():
    global _session_factory
    if _session_factory is None:
        with _session_factory_lock:
            if _session_factory is None:
                engine = make_engine(get_settings().database_url)
                _session_factory = make_session_factory(engine)
    return _session_factory


def get_db():
    db_session = _get_session_factory()()
    try:
        yield db_session
    finally:
        db_session.close()


DBSessionDep = Annotated[DBSessionBase, Depends(get_db)]


# Minimal placeholder UI (full workbench UI lands with the Step 10 gate).
_KEY_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>PortfolioPulse — provider key</title>
<style>
 body { font-family: system-ui, sans-serif; max-width: 32rem; margin: 4rem auto; padding: 0 1rem; }
 input, button { font-size: 1rem; padding: .5rem .75rem; }
 input { width: 100%; box-sizing: border-box; }
 button { margin-top: .75rem; cursor: pointer; }
 #status { margin-top: 1rem; color: #555; white-space: pre-line; }
 label { display: block; margin-top: .75rem; }
</style></head><body>
<h1>PortfolioPulse provider key</h1>
<p>Paste your Google Gemini API key. It is stored locally in <code>.env</code> only,
never echoed back.</p>
<form onsubmit="save(event)">
  <input type="password" id="key" placeholder="GEMINI_API_KEY" autocomplete="off" required>
  <label><input type="checkbox" id="llm" checked> Use Gemini for claim extraction (LLM_PROVIDER)</label>
  <label><input type="checkbox" id="search" checked> Use Gemini grounding for source discovery (SEARCH_PROVIDER)</label>
  <button>Save</button>
</form>
<div id="status"></div>
<script>
async function status() {
  const r = await fetch('/provider-status'); const s = await r.json();
  document.getElementById('status').textContent =
    `providers: llm=${s.llm_provider}, search=${s.search_provider} | key stored: ${s.gemini_key_set ? 'yes' : 'no'}`;
}
async function save(e) {
  e.preventDefault();
  const body = { api_key: document.getElementById('key').value,
                 use_for_llm: document.getElementById('llm').checked,
                 use_for_search: document.getElementById('search').checked };
  const r = await fetch('/api-key', { method: 'POST',
    headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  const out = await r.json();
  if (!r.ok) { document.getElementById('status').textContent = 'error: ' + (out.detail || 'failed'); return; }
  document.getElementById('key').value = '';
  status();
}
status();
</script></body></html>"""


class ApiKeyIn(BaseModel):
    api_key: str = Field(min_length=10, max_length=300)
    use_for_llm: bool = True
    use_for_search: bool = True


@app.get("/health")
def health() -> dict:
    """Liveness probe. Exposes nothing sensitive: no paths, no secrets, no config."""
    return {
        "status": "ok",
        "service": "portfoliopulse",
        "environment": settings.app_env,
    }


@app.get("/key", response_class=HTMLResponse, include_in_schema=False)
def key_page() -> str:
    """Provider-key entry page (placeholder UI until the Step 10 workbench)."""
    return _KEY_PAGE


@app.post("/api-key")
def store_api_key(body: ApiKeyIn) -> dict:
    """Persist the Gemini key to .env; optionally activate it as the provider.

    The key is never returned, logged, or stored in the database.
    """
    update_env_var("GEMINI_API_KEY", body.api_key.strip())
    if body.use_for_llm:
        update_env_var("LLM_PROVIDER", "gemini")
    if body.use_for_search:
        update_env_var("SEARCH_PROVIDER", "gemini")
    get_settings.cache_clear()
    return {
        "ok": True,
        "gemini_key_set": bool(effective_env("GEMINI_API_KEY")),
        "llm_provider": effective_env("LLM_PROVIDER") or settings.llm_provider,
        "search_provider": effective_env("SEARCH_PROVIDER") or settings.search_provider,
    }


@app.get("/provider-status")
def provider_status() -> dict:
    current = get_settings()
    current_key = effective_env("GEMINI_API_KEY") or current.gemini_api_key or ""
    return {
        "llm_provider": effective_env("LLM_PROVIDER") or current.llm_provider,
        "search_provider": effective_env("SEARCH_PROVIDER") or current.search_provider,
        "gemini_model": effective_env("GEMINI_MODEL") or current.gemini_model,
        "gemini_key_set": bool(current_key),
        # masked hint only; never the key itself
        "gemini_key_preview": f"...{current_key[-4:]}" if current_key else None,
    }


# ---------------------------------------------------------------------------
# Review workflow (Step 8): inspect, decide, resolve, approve for export
# ---------------------------------------------------------------------------

_REVIEW_DECISION = Literal["approved", "edited", "rejected", "flagged"]


class ReviewIn(BaseModel):
    target_type: Literal["claim", "hypothesis", "validation_issue"]
    target_id: str = Field(min_length=1, max_length=64)
    decision: _REVIEW_DECISION
    reviewer_notes: str | None = Field(default=None, max_length=4000)
    edited_text: str | None = Field(default=None, max_length=10000)


class IssueStatusIn(BaseModel):
    status: Literal["resolved", "waived"]
    reviewer_notes: str | None = Field(default=None, max_length=4000)


def _map_review_errors(exc: ReviewInputError):
    if isinstance(exc, ReviewNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    return HTTPException(status_code=400, detail=str(exc))


def _claim_payload(session: DBSessionBase, claim: m.Claim) -> dict:
    """Everything an analyst needs to decide: fact, excerpt, provenance, issues."""
    from portfoliopulse.validate import open_issues_for_claim

    return {
        "claim_id": claim.id,
        "claim_text": claim.claim_text,
        "category": claim.category,
        "supporting_excerpt": claim.supporting_excerpt,
        "event_date": claim.event_date.isoformat() if claim.event_date else None,
        "verification_status": claim.verification_status,
        "review_status": claim.review_status,
        "sources": [
            {
                "source_id": s.id,
                "url": s.url,
                "canonical_url": s.canonical_url,
                "title": s.title,
                "publisher": s.publisher,
                "source_type": s.source_type,
                "published_at": s.published_at.isoformat() if s.published_at else None,
                "retrieved_at": s.retrieved_at.isoformat() if s.retrieved_at else None,
                "fetch_status": s.status,
            }
            for s in claim.sources
        ],
        "validation_issues": [
            {
                "issue_id": issue.id,
                "issue_type": issue.issue_type,
                "message": issue.message,
                "review_status": issue.review_status,
            }
            for issue in open_issues_for_claim(session, claim.id)
        ],
    }


def _hypothesis_payload(hypothesis: m.Hypothesis) -> dict:
    return {
        "hypothesis_id": hypothesis.id,
        "title": hypothesis.title,
        "hypothesis_text": hypothesis.hypothesis_text,
        "alternative_explanations": hypothesis.alternative_explanations,
        "missing_evidence": hypothesis.missing_evidence,
        "questions_to_validate": hypothesis.questions_to_validate,
        "evidence_summary": hypothesis.evidence_summary,
        "status": hypothesis.status,
        "supporting_claim_ids": [claim.id for claim in hypothesis.claims],
    }


@app.get("/runs")
def list_runs(db: DBSessionDep) -> dict:
    runs = db.query(m.ResearchRun).order_by(m.ResearchRun.started_at).all()
    return {
        "runs": [
            {
                "run_id": run.id,
                "status": run.status,
                "research_focus": run.research_focus,
                "source_count": run.source_count,
                "claim_count": run.claim_count,
                "hypothesis_count": run.hypothesis_count,
                "started_at": run.started_at.isoformat() if run.started_at else None,
            }
            for run in runs
        ]
    }


@app.get("/runs/{run_id}/review")
def review_payload(run_id: str, db: DBSessionDep) -> dict:
    """Inspection + unresolved overview for one run (the analyst's workbench data)."""
    run = db.get(m.ResearchRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"run {run_id!r} does not exist")
    company = db.get(m.Company, run.company_id)
    claims = db.query(m.Claim).filter_by(run_id=run.id).order_by(m.Claim.created_at).all()
    hypotheses = (
        db.query(m.Hypothesis).filter_by(run_id=run.id).order_by(m.Hypothesis.created_at).all()
    )
    return {
        "run_id": run.id,
        "company_name": company.name if company else None,
        "research_focus": run.research_focus,
        "run_status": run.status,
        "error_summary": run.error_summary,
        "unresolved": unresolved_summary(db, run),
        "claims": [_claim_payload(db, claim) for claim in claims],
        "hypotheses": [_hypothesis_payload(hyp) for hyp in hypotheses],
    }


@app.post("/reviews")
def post_review(body: ReviewIn, db: DBSessionDep) -> dict:
    try:
        if body.target_type == "claim":
            review = review_claim(
                db,
                claim_id=body.target_id,
                decision=body.decision,
                reviewer_notes=body.reviewer_notes,
                edited_text=body.edited_text,
            )
        elif body.target_type == "hypothesis":
            review = review_hypothesis(
                db,
                hypothesis_id=body.target_id,
                decision=body.decision,
                reviewer_notes=body.reviewer_notes,
                edited_text=body.edited_text,
            )
        else:
            raise ReviewInputError(
                "validation issues are resolved via POST /validation-issues/{id}/status"
            )
    except (ReviewInputError, ReviewNotFoundError) as exc:
        raise _map_review_errors(exc) from exc
    db.commit()
    return {
        "id": review.id,
        "target_type": review.target_type,
        "target_id": review.target_id,
        "decision": review.decision,
        "reviewer_notes": review.reviewer_notes,
        "created_at": review.created_at.isoformat(),
    }


@app.post("/validation-issues/{issue_id}/status")
def post_issue_status(issue_id: str, body: IssueStatusIn, db: DBSessionDep) -> dict:
    try:
        issue = resolve_issue(
            db, issue_id=issue_id, status=body.status, reviewer_notes=body.reviewer_notes
        )
    except (ReviewInputError, ReviewNotFoundError) as exc:
        raise _map_review_errors(exc) from exc
    db.commit()
    return {
        "issue_id": issue.id,
        "issue_type": issue.issue_type,
        "review_status": issue.review_status,
    }


# ---------------------------------------------------------------------------
# Research lifecycle (Step 10): companies, runs, run-trigger, approve, export
# ---------------------------------------------------------------------------


class CompanyIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    website: str | None = Field(default=None, max_length=500)
    sector: str | None = Field(default=None, max_length=200)
    country: str | None = Field(default=None, max_length=200)

    @field_validator("name")
    @classmethod
    def _name_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("company name must not be blank")
        return value


class RunIn(BaseModel):
    company_id: str = Field(min_length=1, max_length=64)
    research_focus: str = Field(min_length=3, max_length=500)

    @field_validator("research_focus")
    @classmethod
    def _focus_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("research focus must not be blank")
        return value


class DecisionIn(BaseModel):
    decision: _REVIEW_DECISION
    reviewer_notes: str | None = Field(default=None, max_length=4000)
    edited_text: str | None = Field(default=None, max_length=10000)


def _company_payload(company: m.Company) -> dict:
    return {
        "company_id": company.id,
        "name": company.name,
        "website": company.website,
        "sector": company.sector,
        "country": company.country,
    }


def _run_payload(db: DBSessionBase, run: m.ResearchRun) -> dict:
    company = db.get(m.Company, run.company_id)
    return {
        "run_id": run.id,
        "status": run.status,
        "research_focus": run.research_focus,
        "source_count": run.source_count,
        "claim_count": run.claim_count,
        "hypothesis_count": run.hypothesis_count,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "error_summary": run.error_summary,
        "config_summary": run.config_summary,
        "company": _company_payload(company) if company else None,
    }


def _run_or_404(db: DBSessionBase, run_id: str) -> m.ResearchRun:
    run = db.get(m.ResearchRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"run {run_id!r} does not exist")
    return run


@app.post("/companies")
def create_company(body: CompanyIn, db: DBSessionDep) -> dict:
    company = m.Company(
        id=m.new_id("company"),
        name=body.name.strip(),
        website=(body.website or "").strip() or None,
        sector=(body.sector or "").strip() or None,
        country=(body.country or "").strip() or None,
    )
    db.add(company)
    db.commit()
    return _company_payload(company)


@app.get("/companies")
def list_companies(db: DBSessionDep) -> dict:
    companies = db.query(m.Company).order_by(m.Company.created_at).all()
    return {"companies": [_company_payload(c) for c in companies]}


@app.get("/companies/{company_id}")
def get_company(company_id: str, db: DBSessionDep) -> dict:
    company = db.get(m.Company, company_id)
    if company is None:
        raise HTTPException(status_code=404, detail=f"company {company_id!r} does not exist")
    return _company_payload(company)


def _execute_run(run_id: str) -> None:
    """Background execution (FastAPI BackgroundTasks; an MVP-acceptable, documented
    queue-less choice). Marks itself failed on errors — one bad run never blocks
    another. Providers are resolved at execution time from current settings."""
    db = _get_session_factory()()
    try:
        run = db.get(m.ResearchRun, run_id)
        if run is None or run.status != "queued":  # never double-execute
            return
        company = db.get(m.Company, run.company_id)
        if company is None:
            run.status = "failed"
            run.error_summary = "company disappeared before execution; run abandoned"
            db.commit()
            return
        config = get_settings()
        try:
            search = get_search_provider(config)
            extractor = get_llm_provider(config)
            fetcher, close_fetcher = get_fetcher(config)
        except (SearchProviderUnavailable, LLMProviderUnavailable) as exc:
            run.status = "failed"
            run.error_summary = f"provider not configured: {exc}"
            run.finished_at = utcnow()
            db.commit()
            return
        no_wait = config.search_provider == "demo"  # fixtures need no politeness delay
        # robots.txt applies to real network fetches only; offline fixtures have none
        robots = None if config.search_provider in ("demo", "mock") else RobotsGate()
        try:
            pipeline = ResearchPipeline(
                db,
                run,
                company,
                search_provider=search,
                extractor=extractor,
                fetcher=fetcher,
                throttle=DomainThrottle(0.0 if no_wait else config.min_request_interval_seconds),
                timeout=config.http_timeout_seconds,
                max_sources_per_run=config.max_sources_per_run,
                max_research_rounds=config.max_research_rounds,
                robots=robots,
            )
            pipeline.execute()
            db.commit()
            logger.info("research run %s finished in status %s", run_id, run.status)
        finally:
            close_fetcher()
    except Exception as exc:
        db.rollback()
        run = db.get(m.ResearchRun, run_id)
        if run is not None and run.status not in ("approved", "failed"):
            run.status = "failed"
            run.error_summary = f"pipeline crashed outside a node: {type(exc).__name__}: {exc}"
            run.finished_at = utcnow()
            db.commit()
        logger.exception("research run %s crashed", run_id)
    finally:
        db.close()


@app.post("/research-runs")
def create_run(body: RunIn, background: BackgroundTasks, db: DBSessionDep) -> dict:
    company = db.get(m.Company, body.company_id)
    if company is None:
        raise HTTPException(status_code=404, detail=f"company {body.company_id!r} does not exist")
    run = m.ResearchRun(
        id=m.new_id("run"),
        company_id=company.id,
        research_focus=body.research_focus.strip(),
        status="queued",
    )
    db.add(run)
    db.commit()
    background.add_task(_execute_run, run.id)
    return {
        "run_id": run.id,
        "status": "queued",
        "company_id": company.id,
        "company_name": company.name,
        "message": "Research started in the background; poll GET /research-runs/{run_id}.",
    }


@app.post("/research-runs/{run_id}/resume")
def resume_run(run_id: str, background: BackgroundTasks, db: DBSessionDep) -> dict:
    """Re-enter execution for a run lost to a restart (queued) or a failed diagnosis.

    Every pipeline node is idempotent and persisted, so replay is safe: collected
    sources are kept, already-extracted sources are skipped, validation recomputes.
    """
    run = _run_or_404(db, run_id)
    if run.status not in ("queued", "failed"):
        raise HTTPException(
            status_code=409,
            detail=(
                f"run is in status {run.status}; only queued or failed runs can be resumed. "
                "Awaiting-review and approved runs are already complete."
            ),
        )
    run.status = "queued"
    run.error_summary = None
    db.commit()
    background.add_task(_execute_run, run.id)
    return _run_payload(db, run)


@app.get("/research-runs/{run_id}")
def get_run(run_id: str, db: DBSessionDep) -> dict:
    return _run_payload(db, _run_or_404(db, run_id))


@app.get("/research-runs/{run_id}/sources")
def list_run_sources(run_id: str, db: DBSessionDep) -> dict:
    run = _run_or_404(db, run_id)
    sources = db.query(m.Source).filter_by(run_id=run.id).order_by(m.Source.id).all()
    return {
        "run_id": run.id,
        "sources": [
            {
                "source_id": s.id,
                "url": s.url,
                "canonical_url": s.canonical_url,
                "title": s.title,
                "publisher": s.publisher,
                "source_type": s.source_type,
                "status": s.status,
                "http_status": s.http_status,
                "extraction_status": s.extraction_status,
                "published_at": s.published_at.isoformat() if s.published_at else None,
                "retrieved_at": s.retrieved_at.isoformat() if s.retrieved_at else None,
            }
            for s in sources
        ],
    }


@app.get("/research-runs/{run_id}/claims")
def list_run_claims(run_id: str, db: DBSessionDep) -> dict:
    run = _run_or_404(db, run_id)
    claims = db.query(m.Claim).filter_by(run_id=run.id).order_by(m.Claim.created_at).all()
    return {"run_id": run.id, "claims": [_claim_payload(db, claim) for claim in claims]}


@app.get("/research-runs/{run_id}/hypotheses")
def list_run_hypotheses(run_id: str, db: DBSessionDep) -> dict:
    run = _run_or_404(db, run_id)
    hypotheses = (
        db.query(m.Hypothesis).filter_by(run_id=run.id).order_by(m.Hypothesis.created_at).all()
    )
    return {"run_id": run.id, "hypotheses": [_hypothesis_payload(hyp) for hyp in hypotheses]}


@app.post("/claims/{claim_id}/review")
def post_claim_review(claim_id: str, body: DecisionIn, db: DBSessionDep) -> dict:
    try:
        review = review_claim(
            db,
            claim_id=claim_id,
            decision=body.decision,
            reviewer_notes=body.reviewer_notes,
            edited_text=body.edited_text,
        )
    except (ReviewInputError, ReviewNotFoundError) as exc:
        raise _map_review_errors(exc) from exc
    db.commit()
    return {"review_id": review.id, "target": "claim", "decision": review.decision}


@app.post("/hypotheses/{hypothesis_id}/review")
def post_hypothesis_review(hypothesis_id: str, body: DecisionIn, db: DBSessionDep) -> dict:
    try:
        review = review_hypothesis(
            db,
            hypothesis_id=hypothesis_id,
            decision=body.decision,
            reviewer_notes=body.reviewer_notes,
            edited_text=body.edited_text,
        )
    except (ReviewInputError, ReviewNotFoundError) as exc:
        raise _map_review_errors(exc) from exc
    db.commit()
    return {"review_id": review.id, "target": "hypothesis", "decision": review.decision}


@app.post("/research-runs/{run_id}/approve")
def approve_research_run(run_id: str, db: DBSessionDep) -> dict:
    run = _run_or_404(db, run_id)
    try:
        approve_run(db, run)
    except ApprovalBlocked as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    db.commit()
    return {
        "run_id": run.id,
        "status": run.status,
        "message": "Run approved after completed human review; exports may be generated.",
    }


@app.post("/research-runs/{run_id}/export")
def export_research_run(run_id: str, db: DBSessionDep) -> dict:
    run = _run_or_404(db, run_id)
    try:
        manifest = export_run(db, run, out_root=get_settings().export_dir)
    except ExportBlocked as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ExportWriteError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return manifest


@app.get("/research-runs/{run_id}/exports")
def list_exports(run_id: str, db: DBSessionDep) -> dict:
    run = _run_or_404(db, run_id)
    company = db.get(m.Company, run.company_id)
    manifest_path = run_export_dir(run, company, get_settings().export_dir) / "manifest.json"
    if not manifest_path.exists():
        return {
            "run_id": run.id,
            "exports": None,
            "message": "no exports generated yet; POST /research-runs/{run_id}/export first",
        }
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    return {"run_id": run.id, "exports": manifest}


@app.get("/research-runs/{run_id}/exports/{filename}")
def download_export(run_id: str, filename: str, db: DBSessionDep):
    """Serves exactly the files named in the run's manifest — no traversal."""
    run = _run_or_404(db, run_id)
    company = db.get(m.Company, run.company_id)
    run_dir = run_export_dir(run, company, get_settings().export_dir)
    manifest_path = run_dir / "manifest.json"
    if not manifest_path.exists():
        raise HTTPException(status_code=404, detail="no exports generated yet")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    allowed = set(manifest.get("files", [])) | {"manifest.json"}
    if "/" in filename or "\\" in filename or filename not in allowed:
        raise HTTPException(status_code=404, detail="no such export file for this run")
    path = (run_dir / filename).resolve()
    if path.parent != run_dir.resolve() or not path.is_file():
        raise HTTPException(status_code=404, detail="no such export file for this run")
    return FileResponse(path, filename=filename)


# ---------------------------------------------------------------------------
# Minimal workbench UI: the key workflow only (start → review → approve → export)
# ---------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse, include_in_schema=False)
@app.get("/app", response_class=HTMLResponse, include_in_schema=False)
def workbench() -> str:
    """Minimal workbench: start run → review → approve → export (plus /key)."""
    return _APP_PAGE
