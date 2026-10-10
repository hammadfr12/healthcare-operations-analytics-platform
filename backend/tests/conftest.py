"""Shared pytest fixtures.

The API tests run against the real analytical store rather than a fixture
database, because the thing most worth testing is that the SQL actually binds
against the schema the pipeline builds. A hand-written fixture would agree with
a broken query.

Tests that need the store skip (rather than fail) when it has not been built,
so a fresh checkout can run ``make test`` before ``make pipeline``.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.api.app import create_app  # noqa: E402
from app.config import get_settings  # noqa: E402

# A window well inside the generated dataset, used by most tests.
SAMPLE_START = "2026-07-13"
SAMPLE_END = "2026-10-10"
SAMPLE_WINDOW = f"?start_date={SAMPLE_START}&end_date={SAMPLE_END}"


@pytest.fixture(scope="session")
def store_available() -> bool:
    return get_settings().database_file.exists()


@pytest.fixture(scope="session")
def client(store_available: bool) -> TestClient:
    if not store_available:
        pytest.skip("analytical store not built; run `make pipeline` first")
    return TestClient(create_app())


@pytest.fixture(scope="session")
def window() -> str:
    return SAMPLE_WINDOW
