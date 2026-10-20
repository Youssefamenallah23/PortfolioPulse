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

_APP_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>PortfolioPulse workbench</title>
<style>
 body{font-family:system-ui,sans-serif;max-width:60rem;margin:2rem auto;padding:0 1rem;color:#222;line-height:1.45}
 h1{font-size:1.4rem}h2{font-size:1.05rem;margin:1.2rem 0 .4rem}
 label{display:block;margin:.3rem 0}
 input,textarea,button,select{font:inherit;padding:.4rem .55rem;box-sizing:border-box}
 input,textarea{width:100%;border:1px solid #bbb;border-radius:4px}
 textarea{min-height:4rem}
 button{cursor:pointer;border:1px solid #555;background:#fff;border-radius:4px}
 button.primary{background:#1a4f8b;color:#fff;border-color:#1a4f8b}
 .card{border:1px solid #ddd;border-radius:8px;padding:.8rem;margin:.6rem 0}
 .meta{color:#666;font-size:.85rem}
 .badge{display:inline-block;padding:.1rem .5rem;border-radius:10px;background:#eee;font-size:.8rem}
 .row{display:grid;grid-template-columns:1fr 1fr;gap:.6rem}
 @media (max-width:640px){.row{grid-template-columns:1fr}}
 blockquote{margin:.3rem 0;padding:.3rem .6rem;border-left:3px solid #ccc;color:#555;font-size:.9rem}
 .actions button{margin:.35rem .35rem 0 0;font-size:.85rem}
 a{color:#1a4f8b}
 #status{min-height:1.2rem;font-size:.9rem;color:#0a7d33}
 .err{color:#b3261e}
 ul{margin:.2rem 0;padding-left:1.2rem}
 details summary{cursor:pointer}
</style></head><body>
<h1>PortfolioPulse workbench</h1>
<p class="meta">Evidence-first research; human review gates approval. <a href="/key" target="_blank">Provider key</a></p>
<div id="status"></div>
<section class="card">
 <h2>Start a research run</h2>
 <label>Company (pick an existing one or type a new name)<input id="c-name" list="companies" placeholder="e.g. Northstar Components"></label>
 <datalist id="companies"></datalist>
 <div class="row">
  <label>Company website (optional, new companies)<input id="c-website" placeholder="https://…"></label>
  <label>Sector (optional)<input id="c-sector"></label>
 </div>
 <div class="row">
  <label>Country (optional)<input id="c-country"></label>
  <span></span>
 </div>
 <label>Research focus<textarea id="focus" placeholder="e.g. acquisition integration and geographic expansion"></textarea></label>
 <button class="primary" onclick="startRun()">Start research</button>
 <p class="meta">No Gemini key? Everything still works offline via the demo fixtures.</p>
</section>
<section>
 <h2>Runs</h2>
 <button onclick="loadRuns()">Refresh runs</button>
 <div id="runs"></div>
</section>
<section id="detail"></section>
<script>
const $=id=>document.getElementById(id);
const esc=s=>(s??'').toString().replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let currentRun=null,pollTimer=null;
async function jfetch(url,opts){const r=await fetch(url,opts);const b=await r.json().catch(()=>({}));
 if(!r.ok)throw new Error(b.detail||r.statusText);return b;}
function say(m,err=false){$('status').textContent=m;$('status').className=err?'err':'';}
function stopPoll(){if(pollTimer){clearInterval(pollTimer);pollTimer=null;}}
async function loadCompanies(){try{const d=await jfetch('/companies');
 $('companies').innerHTML=d.companies.map(c=>`<option value="${esc(c.name)}">`).join('');}catch(e){}}
async function loadRuns(){try{const d=await jfetch('/runs');
 $('runs').innerHTML=d.runs.map(r=>`<div class="card"><b>${esc(r.status)}</b> · ${esc(r.research_focus||'')}
  <span class="meta">run ${esc(r.run_id)}</span>
  <div class="meta">sources ${r.source_count} · claims ${r.claim_count} · hypotheses ${r.hypothesis_count}</div>
  <div class="actions"><button onclick="openRun('${esc(r.run_id)}')">Open</button></div></div>`).join('')
  ||'<p class="meta">No runs yet.</p>';}catch(e){say('runs: '+e.message,true);}}
async function startRun(){
 say('starting…');
 const name=$('c-name').value.trim();
 if(!name)return say('company name is required',true);
 try{
  const list=await jfetch('/companies');
  let companyId=(list.companies.find(c=>c.name===name)||{}).company_id;
  if(!companyId){const c=await jfetch('/companies',{method:'POST',
   headers:{'Content-Type':'application/json'},
   body:JSON.stringify({name,website:$('c-website').value.trim()||null,
    sector:$('c-sector').value.trim()||null,country:$('c-country').value.trim()||null})});
   companyId=c.company_id;}
  const body={company_id:companyId,research_focus:$('focus').value.trim()};
  if(body.research_focus.length<3)return say('research focus is required (min 3 chars)',true);
  const run=await jfetch('/research-runs',{method:'POST',headers:{'Content-Type':'application/json'},
   body:JSON.stringify(body)});
  say('run started: '+run.run_id);
  await loadRuns();loadCompanies();openRun(run.run_id);
 }catch(e){say('start failed: '+e.message,true);}}
function openRun(id){currentRun=id;stopPoll();loadRun();pollTimer=setInterval(loadRun,3000);}
const BASE_BY_TARGET={claim:'claims',hypothesis:'hypotheses'};
async function review(targetType,targetId,decision,editedText){
 try{
  await jfetch('/'+BASE_BY_TARGET[targetType]+'/'+targetId+'/review',{method:'POST',
   headers:{'Content-Type':'application/json'},
   body:JSON.stringify({decision,edited_text:editedText||null})});
  say('review recorded: '+decision);loadRun();loadRuns();
 }catch(e){say('review failed: '+e.message,true);}}
async function reviewIssue(issueId,status){
 try{await jfetch('/validation-issues/'+issueId+'/status',{method:'POST',
  headers:{'Content-Type':'application/json'},body:JSON.stringify({status})});
  say('issue '+status);loadRun();}catch(e){say('issue failed: '+e.message,true);}}
async function approveRun(id){
 try{const r=await jfetch('/research-runs/'+id+'/approve',{method:'POST'});
  say(r.message);loadRun();loadRuns();}catch(e){say('approve blocked: '+e.message,true);}}
async function exportRun(id){
 try{await jfetch('/research-runs/'+id+'/export',{method:'POST'});
  say('exports generated — see download links');loadRuns();}catch(e){say('export failed: '+e.message,true);}}
async function loadRun(){
 if(!currentRun)return;
 let run,rev,exportsInfo=null;
 try{
  run=await jfetch('/research-runs/'+currentRun);
  rev=await jfetch('/runs/'+currentRun+'/review');
  if(run.status==='approved')try{exportsInfo=await jfetch('/research-runs/'+currentRun+'/exports');}catch(e){}
 }catch(e){say('detail: '+e.message,true);return;}
 let html=`<div class="card"><b>${esc((run.company||{}).name||run.run_id)}</b> — ${esc(run.research_focus)}
  <span class="badge">${esc(run.status)}</span>
  <div class="meta">sources ${run.source_count} · claims ${run.claim_count} · hypotheses ${run.hypothesis_count}</div>`;
 if(run.error_summary)html+=`<p class="err">${esc(run.error_summary)}</p>`;
 if(run.status==='queued'||run.status==='running')
  html+='<p class="meta">Working… this panel refreshes automatically.</p>';
 html+='<div class="actions">';
 if(run.status==='awaiting_review'||run.status==='approved'){
  html+=`<button class="primary" onclick="approveRun('${esc(run.run_id)}')">Approve for export</button>`;
  html+=`<button onclick="exportRun('${esc(run.run_id)}')">Generate exports</button>`;}
 html+='</div>';
 if(exportsInfo&&exportsInfo.exports){
  html+='<p>Exports: '+exportsInfo.exports.files.map(f=>
   `<a href="/research-runs/${esc(run.run_id)}/exports/${esc(f)}" target="_blank">${esc(f)}</a>`).join(' · ')+'</p>';}
 if(rev){
  const u=rev.unresolved||{};
  html+=`<p class="meta">Open items: ${esc(JSON.stringify(u))}</p>`;
  html+=rev.claims.map(c=>`<div class="card">
   <b>Claim</b> ${esc(c.claim_text)}<br><span class="meta">category ${esc(c.category)} ·
   verification ${esc(c.verification_status)} · review ${esc(c.review_status)}</span>
   <blockquote>excerpt: ${esc(c.supporting_excerpt)}</blockquote>
   <ul>${c.sources.map(s=>`<li class="meta"><a href="${esc(s.canonical_url)}" target="_blank">${esc(s.publisher||s.canonical_url)}</a>
    (retrieved ${esc(s.retrieved_at||'?')}, fetch ${esc(s.fetch_status)})</li>`).join('')}</ul>
   ${(c.validation_issues||[]).map(i=>`<div class="meta">issue ${esc(i.issue_type)}: ${esc(i.message||'')}
    <button onclick="reviewIssue('${esc(i.issue_id)}','resolved')">resolved</button>
    <button onclick="reviewIssue('${esc(i.issue_id)}','waived')">waived</button></div>`).join('')}
   <div class="actions">
    <button onclick="review('claim','${esc(c.claim_id)}','approved')">Approve</button>
    <button onclick="review('claim','${esc(c.claim_id)}','edited',prompt('Corrected wording:',${JSON.stringify(c.claim_text)}))">Edit</button>
    <button onclick="review('claim','${esc(c.claim_id)}','rejected')">Reject</button>
    <button onclick="review('claim','${esc(c.claim_id)}','flagged')">Flag</button>
   </div></div>`).join('');
  html+=rev.hypotheses.map(h=>`<div class="card">
   <b>Hypothesis</b> ${esc(h.title)} <span class="badge">${esc(h.status)}</span>
   <blockquote>${esc(h.hypothesis_text)}</blockquote>
   <div class="meta">alternatives: ${esc((h.alternative_explanations||[]).join(' | '))}<br>
    missing evidence: ${esc((h.missing_evidence||[]).join(' | '))}</div>
   <div class="actions">
    <button onclick="review('hypothesis','${esc(h.hypothesis_id)}','approved')">Approve</button>
    <button onclick="review('hypothesis','${esc(h.hypothesis_id)}','edited',prompt('Corrected wording:',${JSON.stringify(h.hypothesis_text)}))">Edit</button>
    <button onclick="review('hypothesis','${esc(h.hypothesis_id)}','rejected')">Reject</button>
    <button onclick="review('hypothesis','${esc(h.hypothesis_id)}','flagged')">Flag</button>
   </div></div>`).join('');}
 html+='</div>';
 $('detail').innerHTML=html;
 if(run.status!=='queued'&&run.status!=='running')stopPoll();}
loadCompanies();loadRuns();
</script></body></html>"""


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
@app.get("/app", response_class=HTMLResponse, include_in_schema=False)
def workbench() -> str:
    """Minimal workbench: start run → review → approve → export (plus /key)."""
    return _APP_PAGE
