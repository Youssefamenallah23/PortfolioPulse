"""Database engine and session setup.

Timestamp convention: SQLite has no timezone-aware types, so all datetimes are
stored as *naive UTC* (`utcnow()`). Convert to aware UTC only at output edges.
"""

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from portfoliopulse.models import Base, utcnow


def make_engine(url: str) -> Engine:
    if url == "sqlite:///:memory:":
        # One shared connection: every session (incl. TestClient worker threads)
        # sees the same in-memory database instead of a fresh empty one.
        engine = create_engine(url, connect_args={"check_same_thread": False}, poolclass=StaticPool)
    else:
        engine = create_engine(
            url, connect_args={"check_same_thread": False} if url.startswith("sqlite") else {}
        )
    if url.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def _enable_fk(dbapi_connection, _record):  # pragma: no cover - trivial pragma
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    Base.metadata.create_all(engine)
    return engine


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False)


# Re-exported so callers can use `from portfoliopulse.db import Session` in type hints.
__all__ = ["Session", "make_engine", "make_session_factory", "utcnow"]
