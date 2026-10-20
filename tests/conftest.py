"""Shared fixtures: in-memory SQLite engine + session per test."""

import pytest

from portfoliopulse.db import make_engine, make_session_factory


@pytest.fixture
def engine():
    return make_engine("sqlite:///:memory:")


@pytest.fixture
def session(engine):
    factory = make_session_factory(engine)
    s = factory()
    yield s
    s.close()
