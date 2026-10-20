"""Structured logging with run IDs.

Deliberately small: a run-scoped contextvar so log lines can carry `run_id`
without passing it through every call site. Never log secrets or full page
content; callers are responsible for logging only safe diagnostics.
"""

import contextvars
import logging
from datetime import UTC, datetime

run_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("run_id", default=None)


class RunAwareFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        record.run_id = run_id_var.get() or "-"
        # ponytail: key=value logging, swap for json.dumps structured lines if a
        # log sink ever needs to parse them.
        record.asctime_compact = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        return f"{record.asctime_compact} {record.levelname} run={record.run_id} {record.name}: {record.getMessage()}"


def setup_logging(level: int = logging.INFO) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(RunAwareFormatter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)
    # Noise reduction for libraries that log per-request.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
