"""Tests for configuration loading: defaults, overrides, and rejection of invalid values."""

import pytest
from pydantic import ValidationError

from portfoliopulse.config import Settings


@pytest.fixture
def clean_env(monkeypatch):
    """Remove settings-relevant env vars so defaults are actually tested."""
    keys = [
        "APP_ENV",
        "DATABASE_URL",
        "SEARCH_PROVIDER",
        "LLM_PROVIDER",
        "MAX_SOURCES_PER_RUN",
        "MAX_RESEARCH_ROUNDS",
        "HTTP_TIMEOUT_SECONDS",
        "MIN_REQUEST_INTERVAL_SECONDS",
        "EXPORT_DIR",
    ]
    for key in keys:
        monkeypatch.delenv(key, raising=False)
    return monkeypatch


def test_defaults(clean_env):
    s = Settings(_env_file=None)
    assert s.app_env == "development"
    assert s.database_url == "sqlite:///./portfoliopulse.db"
    assert s.search_provider == "mock"
    assert s.llm_provider == "mock"
    assert s.max_sources_per_run == 20
    assert s.max_research_rounds == 2
    assert s.http_timeout_seconds == 15
    assert s.min_request_interval_seconds == 1
    assert s.export_dir == "./artifacts"


def test_env_overrides_defaults(clean_env):
    clean_env.setenv("APP_ENV", "staging")
    clean_env.setenv("MAX_SOURCES_PER_RUN", "5")
    clean_env.setenv("SEARCH_PROVIDER", "tavily")
    s = Settings(_env_file=None)
    assert s.app_env == "staging"
    assert s.max_sources_per_run == 5
    assert s.search_provider == "tavily"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("MAX_SOURCES_PER_RUN", "-1"),
        ("MAX_RESEARCH_ROUNDS", "0"),
        ("HTTP_TIMEOUT_SECONDS", "0"),
        ("MIN_REQUEST_INTERVAL_SECONDS", "-5"),
    ],
)
def test_invalid_values_fail_clearly(clean_env, field, value):
    clean_env.setenv(field, value)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_import_app_makes_no_network(monkeypatch):
    """Importing the app must not perform network calls or start research jobs."""
    calls = []
    monkeypatch.setattr("httpx.Client.request", lambda *a, **k: calls.append(1))

    import portfoliopulse.main  # noqa: F401

    assert calls == []
