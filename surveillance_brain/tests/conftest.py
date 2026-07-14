"""
tests/conftest.py
=================
Pytest fixtures shared by all smoke tests.

Per spec: "These tests should simply verify that the application boots
successfully, database connections are established, and the primary API
endpoints return 200 OK responses. Deep logical or unit testing is out
of scope for this phase."

Strategy:
    - Use FastAPI's TestClient (sync wrapper over the async app).
    - Tests run against whatever DB/Redis the .env points at — no test
      containers, no mocking.  This keeps the test surface small and
      matches the "smoke" intent.
    - If DB/Redis is unreachable, tests SKIP rather than fail — so
      `pytest` can run in CI without infrastructure.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# Make the project root importable
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


@pytest.fixture(scope="session")
def app():
    """Yield the FastAPI app instance."""
    # Disable the midnight flush scheduler inside tests so we don't
    # accidentally mutate the DB during a test run.
    os.environ["ENABLE_MIDNIGHT_FLUSH"] = "0"
    # No background consolidation while tests assert on identity counts.
    os.environ["CONSOLIDATE_ENABLE"] = "0"
    # Embedded Qdrant allows ONE process — the live Brain may hold the real
    # store's lock, so tests run against their own throwaway vector store.
    import tempfile
    os.environ["QDRANT_LOCAL_PATH"] = tempfile.mkdtemp(prefix="qdrant_test_")

    from api.main import app as _app
    yield _app


@pytest.fixture(scope="session")
def client(app):
    """FastAPI TestClient — sync interface over the async app."""
    from fastapi.testclient import TestClient
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="session")
def admin_auth():
    """HTTP Basic auth tuple (user, pass) from config."""
    import config
    return (config.ADMIN_USERNAME, config.ADMIN_PASSWORD)
