"""Step 10 acceptance tests: full API workbench flow, offline (demo providers)."""

import pytest
from fastapi.testclient import TestClient

from portfoliopulse import main
from portfoliopulse.config import get_settings
from portfoliopulse.db import make_engine, make_session_factory


@pytest.fixture
def client(monkeypatch, tmp_path):
    """TestClient bound to a fresh in-memory DB; demo providers; tmp exports dir."""
    engine = make_engine("sqlite:///:memory:")
    factory = make_session_factory(engine)
    monkeypatch.setattr(main, "_session_factory", factory)
    monkeypatch.setenv("SEARCH_PROVIDER", "demo")
    monkeypatch.setenv("LLM_PROVIDER", "demo")
    monkeypatch.setenv("EXPORT_DIR", str(tmp_path))
    get_settings.cache_clear()
    test_client = TestClient(main.app)
    try:
        yield test_client, tmp_path
    finally:
        test_client.close()
        get_settings.cache_clear()


def _make_company(client) -> str:
    response = client.post(
        "/companies",
        json={"name": "Northstar Components", "website": "https://www.northstar-components.test"},
    )
    assert response.status_code == 200, response.text
    return response.json()["company_id"]


def _full_review(client, run_id: str) -> list[str] | None:
    """Review every pending claim + draft hypothesis + open issue; returns pending claim ids."""
    payload = client.get(f"/runs/{run_id}/review").json()
    claims = client.get(f"/research-runs/{run_id}/claims").json()["claims"]
    pending = [claim["claim_id"] for claim in claims if claim["review_status"] == "pending"]
    for claim_id in pending:
        assert (
            client.post(f"/claims/{claim_id}/review", json={"decision": "approved"}).status_code
            == 200
        )
    hyps = client.get(f"/research-runs/{run_id}/hypotheses").json()["hypotheses"]
    for hyp in hyps:
        if hyp["status"] == "draft":
            assert (
                client.post(
                    f"/hypotheses/{hyp['hypothesis_id']}/review", json={"decision": "approved"}
                ).status_code
                == 200
            )
    issue_ids = {
        issue["issue_id"]
        for claim in payload["claims"]
        for issue in claim.get("validation_issues", [])
        if issue.get("review_status") == "pending"
    }
    for issue_id in issue_ids:
        assert (
            client.post(
                f"/validation-issues/{issue_id}/status", json={"status": "resolved"}
            ).status_code
            == 200
        )
    return pending


# --- companies ----------------------------------------------------------------------


def test_company_crud_and_validation(client):
    test_client, _tmp = client
    company_id = _make_company(test_client)
    listed = test_client.get("/companies").json()["companies"]
    assert any(c["company_id"] == company_id for c in listed)
    got = test_client.get(f"/companies/{company_id}").json()
    assert got["name"] == "Northstar Components"
    assert test_client.get("/companies/company_missing").status_code == 404
    # validation: empty name and over-long name are rejected by the schema
    assert test_client.post("/companies", json={"name": "  "}).status_code == 422
    assert test_client.post("/companies", json={"name": "x" * 250}).status_code == 422


# --- full lifecycle (demo mode, offline) ---------------------------------------------


def test_full_lifecycle_offline_demo(client):
    test_client, tmp = client
    company_id = _make_company(test_client)
    created = test_client.post(
        "/research-runs",
        json={"company_id": company_id, "research_focus": "acquisition integration"},
    )
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["status"] == "queued"
    run_id = body["run_id"]

    # BackgroundTasks run synchronously under TestClient → already awaiting review.
    detail = test_client.get(f"/research-runs/{run_id}").json()
    assert detail["status"] == "awaiting_review", detail
    assert detail["company"]["name"] == "Northstar Components"
    assert detail["error_summary"] is None
    assert detail["source_count"] == 4  # website + 3 search hit pages, all demo fixtures
    assert detail["hypothesis_count"] == 2  # strategy (profile + financial) and expansion themes

    claims = test_client.get(f"/research-runs/{run_id}/claims").json()["claims"]
    categories = {claim["category"] for claim in claims}
    assert {"company_profile", "acquisition", "geographic_expansion"} <= categories
    for claim in claims:
        assert claim["sources"], "every claim carries provenance"
    statuses = [claim["verification_status"] for claim in claims]
    assert set(statuses) <= {"supported", "conflicting"}
    assert statuses.count("conflicting") == 2  # 48 vs 60 million price pair; no winner picked

    sources = test_client.get(f"/research-runs/{run_id}/sources").json()["sources"]
    assert all(s["status"] == "fetched" for s in sources), sources
    assert {s["canonical_url"] for s in sources} >= {
        "https://www.northstar-components.test/investors/news/acquisition-meridian",
        "https://trade-news.test/northstar-expansion",
    }

    pending = _full_review(test_client, run_id)
    assert pending is not None and len(pending) == len(claims)

    # approve is blocked only by pending items; after the review sweep it must pass
    approved = test_client.post(f"/research-runs/{run_id}/approve")
    assert approved.status_code == 200
    assert approved.json()["status"] == "approved"

    manifest = test_client.post(f"/research-runs/{run_id}/export").json()
    assert manifest["approval_status"] == "approved"
    assert {"analyst_brief.md", "research_pack.xlsx"} <= set(manifest["files"])

    exports = test_client.get(f"/research-runs/{run_id}/exports").json()["exports"]
    assert exports["run_id"] == run_id
    brief = test_client.get(f"/research-runs/{run_id}/exports/analyst_brief.md")
    assert brief.status_code == 200
    assert "Northstar Components" in brief.text
    assert "## 9. Sources and retrieval date" in brief.text

    workbook_path = tmp / manifest["directory"] / "research_pack.xlsx"
    assert workbook_path.name == "research_pack.xlsx" and workbook_path.exists()


def test_export_blocked_before_approval_and_unknown_runs(client):
    test_client, _tmp = client
    company_id = _make_company(test_client)
    run_id = test_client.post(
        "/research-runs",
        json={"company_id": company_id, "research_focus": "expansion only"},
    ).json()["run_id"]

    # not approved yet → 400; no exports → soft None payload; file download → 404
    assert test_client.post(f"/research-runs/{run_id}/export").status_code == 400
    assert (
        test_client.post(f"/research-runs/{run_id}/approve").status_code == 400
    )  # pending claims block approval
    assert test_client.get(f"/research-runs/{run_id}/exports").json()["exports"] is None
    assert test_client.get(f"/research-runs/{run_id}/exports/analyst_brief.md").status_code == 404

    # unknown ids across the lifecycle endpoints (GET routes)
    for path in (
        "/research-runs/run_missing",
        "/research-runs/run_missing/sources",
        "/research-runs/run_missing/claims",
        "/research-runs/run_missing/hypotheses",
        "/research-runs/run_missing/exports",
    ):
        assert test_client.get(path).status_code == 404, path
    # POST routes: 404 for unknown runs, never a 500
    for path in ("/research-runs/run_missing/approve", "/research-runs/run_missing/export"):
        assert test_client.post(path).status_code == 404, path
    assert (
        test_client.post(
            "/research-runs",
            json={"company_id": "company_missing", "research_focus": "valid focus"},
        ).status_code
        == 404
    )
    assert (
        test_client.post("/claims/claim_missing/review", json={"decision": "approved"}).status_code
        == 404
    )
    assert (
        test_client.post(
            "/hypotheses/hyp_missing/review", json={"decision": "approved"}
        ).status_code
        == 404
    )
    # schema validation on run input
    assert (
        test_client.post(
            "/research-runs", json={"company_id": company_id, "research_focus": "x"}
        ).status_code
        == 422
    )


def test_reviews_via_specific_endpoints_and_approval_gate(client):
    test_client, _tmp = client
    company_id = _make_company(test_client)
    run_id = test_client.post(
        "/research-runs",
        json={"company_id": company_id, "research_focus": "acquisition integration"},
    ).json()["run_id"]
    claims = test_client.get(f"/research-runs/{run_id}/claims").json()["claims"]

    # reviewer edit flows straight through and changes the stored wording
    target = next(claim for claim in claims if claim["category"] == "acquisition")
    response = test_client.post(
        f"/claims/{target['claim_id']}/review",
        json={"decision": "edited", "edited_text": target["claim_text"] + " (reviewer confirmed.)"},
    )
    assert response.status_code == 200
    updated = test_client.get(f"/research-runs/{run_id}/claims").json()["claims"]
    edited = next(claim for claim in updated if claim["claim_id"] == target["claim_id"])
    assert edited["review_status"] == "edited"
    assert edited["claim_text"].endswith("(reviewer confirmed.)")

    # invalid decisions / edits are 4xx, not 422-server-crash paths
    assert (
        test_client.post(
            f"/claims/{target['claim_id']}/review", json={"decision": "approve!"}
        ).status_code
        == 422
    )
    assert (
        test_client.post(
            f"/claims/{target['claim_id']}/review", json={"decision": "edited"}
        ).status_code
        == 400
    )  # edit without edited_text
    assert (
        test_client.post(
            f"/claims/{target['claim_id']}/review", json={"decision": "approved"}
        ).status_code
        == 200
    )


def test_vanished_provider_fails_the_run_not_the_api(client, monkeypatch):
    test_client, _tmp = client
    monkeypatch.setenv("LLM_PROVIDER", "no_such_provider")
    get_settings.cache_clear()
    company_id = _make_company(test_client)
    run_id = test_client.post(
        "/research-runs",
        json={"company_id": company_id, "research_focus": "anything"},
    ).json()["run_id"]
    detail = test_client.get(f"/research-runs/{run_id}").json()
    assert detail["status"] == "failed"
    assert "provider not configured" in detail["error_summary"]
    assert "GEMINI_API_KEY" in detail["error_summary"] or "LLM_PROVIDER" in detail["error_summary"]
    get_settings.cache_clear()


def test_workbench_page_and_health(client):
    test_client, _tmp = client
    page = test_client.get("/")
    assert page.status_code == 200
    assert "PortfolioPulse workbench" in page.text
    assert test_client.get("/app").status_code == 200
    assert test_client.get("/health").json()["status"] == "ok"


def test_export_manifest_download_guard(client):
    test_client, _tmp = client
    company_id = _make_company(test_client)
    run_id = test_client.post(
        "/research-runs",
        json={"company_id": company_id, "research_focus": "acquisition integration"},
    ).json()["run_id"]
    _full_review(test_client, run_id)
    test_client.post(f"/research-runs/{run_id}/approve")
    test_client.post(f"/research-runs/{run_id}/export")
    # traversal and unknown filenames never escape the manifest list
    for bad in ("..%2Fmain.py", "secret.json", "..\\main.py"):
        assert test_client.get(f"/research-runs/{run_id}/exports/{bad}").status_code == 404, bad
    # the real manifest is downloadable
    assert test_client.get(f"/research-runs/{run_id}/exports/manifest.json").status_code == 200


def test_resume_requeues_failed_run(client, monkeypatch):
    test_client, _tmp = client
    company_id = _make_company(test_client)
    run_focus = {"company_id": company_id, "research_focus": "acquisition integration"}
    # control run: completes normally and can never be resumed
    ok_id = test_client.post("/research-runs", json=run_focus).json()["run_id"]
    assert test_client.get(f"/research-runs/{ok_id}").json()["status"] == "awaiting_review"
    assert test_client.post(f"/research-runs/{ok_id}/resume").status_code == 409

    # a run that failed on a broken provider...
    monkeypatch.setenv("LLM_PROVIDER", "no_such_provider")
    get_settings.cache_clear()
    failed_id = test_client.post("/research-runs", json=run_focus).json()["run_id"]
    assert test_client.get(f"/research-runs/{failed_id}").json()["status"] == "failed"

    # ...resumes honestly (still broken → failed again, with a reason)...
    resumed = test_client.post(f"/research-runs/{failed_id}/resume")
    assert resumed.status_code == 200
    detail = test_client.get(f"/research-runs/{failed_id}").json()
    assert detail["status"] == "failed"
    assert "provider not configured" in detail["error_summary"]

    # ...and recovers completely once the provider is sane again (idempotent replay)
    monkeypatch.setenv("LLM_PROVIDER", "demo")
    get_settings.cache_clear()
    assert test_client.post(f"/research-runs/{failed_id}/resume").status_code == 200
    detail = test_client.get(f"/research-runs/{failed_id}").json()
    assert detail["status"] == "awaiting_review", detail
    # collected sources were kept instead of refetched, and claims finally landed
    assert detail["source_count"] == 4
    assert detail["claim_count"] > 0
    assert test_client.post(f"/research-runs/{failed_id}/resume").status_code == 409
