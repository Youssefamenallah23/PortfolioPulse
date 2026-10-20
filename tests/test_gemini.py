"""Gemini adapter + API-key endpoint tests.

Real API calls are never made in tests: Gemini client objects are fakes that
mimic the documented google-genai response shape. Offline mode remains the CI oracle.
"""

import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from portfoliopulse import config as cfg
from portfoliopulse.collect import get_search_provider
from portfoliopulse.extract import LLMProviderUnavailable, get_llm_provider
from portfoliopulse.gemini import GeminiExtractor, GeminiSearchProvider


class FakeModels:
    """minimal generate_content stand-in capturing calls, returning canned text."""

    def __init__(self, response: object):
        self.response = response
        self.calls: list[dict] = []

    def generate_content(self, *, model, contents, config):
        self.calls.append({"model": model, "contents": contents, "config": config})
        return self.response


def make_extractor(response, **kwargs):
    if isinstance(response, str):
        response = SimpleNamespace(text=response)
    client = SimpleNamespace(models=FakeModels(response))
    return GeminiExtractor(client=client, **kwargs), client.models


def make_search(response, **kwargs):
    client = SimpleNamespace(models=FakeModels(response))
    return GeminiSearchProvider(client=client, **kwargs), client.models


# --- extraction adapter -----------------------------------------------------------


def test_extractor_returns_raw_model_output_and_reuses_step4_prompt():
    extractor, models = make_extractor('{"claims": []}')
    raw = extractor.extract("source body", focus="focus", publisher="Pub")
    assert raw == '{"claims": []}'
    call = models.calls[0]
    assert call["model"] == "gemini-3.5-flash-lite"
    # the untrusted-document markers and focus rule ride along from Step 4
    assert "UNTRUSTED SOURCE DOCUMENT START" in call["contents"]
    assert "focus" in call["contents"]
    # JSON mode configured (never free-text parsing); SDK may normalise the attr name
    assert call["config"].response_mime_type == "application/json"


def test_extractor_retry_prompt_carries_the_validation_error():
    extractor, models = make_extractor("{}")
    extractor.extract("text", focus="f", publisher=None, repair_of="old", error="bad date")
    assert "RETRY INSTRUCTIONS" in models.calls[0]["contents"]
    assert "bad date" in models.calls[0]["contents"]


def test_extractor_empty_completion_is_a_hard_error():
    extractor, _ = make_extractor(SimpleNamespace(text=None))
    with pytest.raises(RuntimeError, match="empty completion"):
        extractor.extract("text", focus="f", publisher=None)


def test_extractor_without_key_raises_actionable_error():
    with pytest.raises(LLMProviderUnavailable, match="GEMINI_API_KEY"):
        GeminiExtractor(api_key="")


# --- grounded search adapter -------------------------------------------------------


def grounded_response(*hits):
    chunks = [SimpleNamespace(web=SimpleNamespace(uri=uri, title=title)) for uri, title in hits]
    return SimpleNamespace(
        candidates=[SimpleNamespace(grounding_metadata=SimpleNamespace(grounding_chunks=chunks))]
    )


def test_search_provider_maps_grounding_chunks_to_hits():
    provider, models = make_search(
        grounded_response(("https://a.example/1", "One"), ("https://a.example/2", None))
    )
    hits = provider.search("Co", "focus", limit=10)
    assert [h.url for h in hits] == ["https://a.example/1", "https://a.example/2"]
    assert hits[0].source_type == "search_grounding"
    assert models.calls[0]["config"].tools  # grounded with the Google Search tool


def test_search_provider_dedupes_caps_and_skips_uri_less_chunks():
    response = grounded_response(
        ("https://a.example/1", "One"),
        ("https://a.example/1", "One again"),  # duplicate
        (None, "no uri"),  # unusable chunk
        ("https://a.example/3", "Three"),
    )
    provider, _ = make_search(response)
    hits = provider.search("Co", "focus", limit=1)
    assert [h.url for h in hits] == ["https://a.example/1"]


def test_search_provider_empty_grounding_is_empty_not_invented():
    provider, _ = make_search(SimpleNamespace(candidates=[]))
    assert provider.search("Co", "focus", limit=5) == []


# --- factory branches ---------------------------------------------------------------


def test_factories_construct_gemini_providers_from_settings():
    settings = SimpleNamespace(
        llm_provider="gemini", search_provider="gemini", gemini_api_key="k", gemini_model="m"
    )
    assert isinstance(get_llm_provider(settings), GeminiExtractor)
    assert isinstance(get_search_provider(settings), GeminiSearchProvider)


def test_factories_require_a_key_for_gemini():
    settings = SimpleNamespace(
        llm_provider="gemini", search_provider="gemini", gemini_api_key="", gemini_model="m"
    )
    with pytest.raises(LLMProviderUnavailable, match="GEMINI_API_KEY"):
        get_llm_provider(settings)
    with pytest.raises(LLMProviderUnavailable, match="GEMINI_API_KEY"):
        get_search_provider(settings)


def test_factories_still_raise_actionable_errors_for_unknown_names():
    settings = SimpleNamespace(llm_provider="skynet")
    with pytest.raises(LLMProviderUnavailable, match="skynet"):
        get_llm_provider(settings)


# --- /key page + API-key endpoint ---------------------------------------------------


@pytest.fixture()
def api_client(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "ENV_PATH", str(tmp_path / ".env"))
    cfg.get_settings.cache_clear()
    yield TestClient(__import__("portfoliopulse.main", fromlist=["app"]).app)
    cfg.get_settings.cache_clear()


def test_key_page_serves_an_input_field(api_client):
    html = api_client.get("/key").text
    assert 'type="password"' in html
    assert "/api-key" in html  # the form posts to the endpoint


def test_post_api_key_persists_to_env_and_activates_providers(api_client, tmp_path):
    response = api_client.post(
        "/api-key",
        json={"api_key": "AIza" + "x" * 30, "use_for_llm": True, "use_for_search": True},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["gemini_key_set"] is True
    assert body["llm_provider"] == "gemini"
    assert body["search_provider"] == "gemini"
    content = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "GEMINI_API_KEY=AIza" in content  # written to file...
    assert "x" * 30 not in response.text  # ...but NEVER echoed in the response


def test_post_api_key_updates_existing_value_without_duplicates(api_client, tmp_path):
    (tmp_path / ".env").write_text("APP_ENV=development\nGEMINI_API_KEY=old_key_value\n")
    api_client.post("/api-key", json={"api_key": "AIza" + "y" * 30, "use_for_llm": False})
    lines = (tmp_path / ".env").read_text(encoding="utf-8").splitlines()
    key_lines = [line for line in lines if line.startswith("GEMINI_API_KEY=")]
    assert key_lines == [f"GEMINI_API_KEY=AIza{'y' * 30}"]  # replaced in place, once
    assert "APP_ENV=development" in lines  # untouched


def test_post_api_key_rejects_short_keys(api_client):
    assert api_client.post("/api-key", json={"api_key": "short"}).status_code == 422


def test_provider_status_never_leaks_the_full_key(api_client):
    api_client.post("/api-key", json={"api_key": "AIza" + "z" * 30, "use_for_llm": False})
    status = api_client.get("/provider-status").json()
    assert status["gemini_key_set"] is True
    assert status["gemini_key_preview"] == "..." + "z" * 4  # masked hint only
    assert "z" * 30 not in json.dumps(status)
