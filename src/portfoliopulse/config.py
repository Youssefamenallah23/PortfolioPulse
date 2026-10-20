"""Central configuration, loaded from environment variables with safe defaults.

Secrets are accepted WITHOUT storage concerns only through .env / process env;
nothing sensitive is persisted to the database or echoed in logs.
"""

import os
from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

ENV_PATH = ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_env: str = "development"
    # SQLite by default until the working MVP proves a need for more.
    database_url: str = "sqlite:///./portfoliopulse.db"

    # Provider selection. "mock" keeps the whole pipeline offline and deterministic.
    search_provider: str = "mock"
    llm_provider: str = "mock"

    # Gemini credentials (only read when a *_provider is "gemini" or for the status
    # page). Set via the /key page, .env, or an environment variable.
    gemini_api_key: str = ""
    gemini_model: str = "gemini-3.5-flash-lite"

    # Hard limits: enforced later in collection and orchestration, not programmable here.
    max_sources_per_run: int = Field(default=20, ge=1)
    max_research_rounds: int = Field(default=2, ge=1)
    http_timeout_seconds: float = Field(default=15, gt=0)
    min_request_interval_seconds: float = Field(default=1, ge=0)

    export_dir: str = "./artifacts"


@lru_cache
def get_settings() -> Settings:
    return Settings()


def effective_env(name: str, *, path: str | None = None) -> str | None:
    """Current value of an environment variable: process env first, then .env.

    Lets endpoints that just wrote .env (e.g. the key page) report fresh values
    without restarting; OS process env always wins, matching pydantic-settings.
    """
    value = os.environ.get(name)
    if value:
        return value
    target = path or ENV_PATH
    if not Path(target).exists():
        return None
    for line in Path(target).read_text(encoding="utf-8").splitlines():
        if line.partition("=")[0].strip() == name:
            return line.partition("=")[2] or None
    return None


def update_env_var(name: str, value: str, *, path: str | None = None) -> None:
    """Upsert a single KEY=value line in .env and invalidate the settings cache.

    Local-tool persistence for provider keys typed into the /key page; secrets never
    reach the database, logs, or API responses.
    """
    target = path or ENV_PATH
    line = f"{name}={value}"
    existing = (
        Path(target).read_text(encoding="utf-8").splitlines() if Path(target).exists() else []
    )
    replaced = False
    out_lines: list[str] = []
    for current in existing:
        if current.split("=", 1)[0].strip() == name:
            if not replaced:
                out_lines.append(line)
                replaced = True
            # drop duplicate definitions of the same variable
        else:
            out_lines.append(current)
    if not replaced:
        out_lines.append(line)
    Path(target).write_text("\n".join(out_lines) + "\n", encoding="utf-8")
    get_settings.cache_clear()
