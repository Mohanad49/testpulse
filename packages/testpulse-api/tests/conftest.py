"""Test app wiring.

The app is built against a temporary SQLite database per test by overriding the
engine and session dependencies. Nothing here mocks the query layer: the point of
these tests is that the endpoints return what the real metrics engine computes
from really stored rows, and a mock would only assert that the mock works.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from testpulse_core.config import Settings
from testpulse_core.models import TestResult, TestRun, TestStatus
from testpulse_core.storage.db import create_db_engine, create_session_factory, session_scope
from testpulse_core.storage.repository import store_run
from testpulse_core.storage.schema import Base

from testpulse_api.deps import get_config, get_engine, get_session
from testpulse_api.main import create_app

CORE_FIXTURES = (
    Path(__file__).resolve().parents[2] / "testpulse-core" / "tests" / "fixtures"
)
START = datetime(2026, 7, 1, tzinfo=UTC)

STATUSES = {
    "P": TestStatus.PASSED,
    "F": TestStatus.FAILED,
    "E": TestStatus.ERROR,
    "S": TestStatus.SKIPPED,
}


@pytest.fixture
def engine(tmp_path):
    engine = create_db_engine(f"sqlite:///{tmp_path / 'api.db'}")
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def client(engine):
    app = create_app()

    def override_session():
        session: Session = create_session_factory(engine)()
        try:
            yield session
        finally:
            session.close()

    def override_config() -> Settings:
        # Must be a zero-argument callable. Passing the Settings class itself
        # makes FastAPI introspect its signature, which includes
        # pydantic-settings' internal `_env_file` forward reference, and every
        # endpoint depending on config then fails to build.
        return Settings()

    app.dependency_overrides[get_session] = override_session
    app.dependency_overrides[get_engine] = lambda: engine
    app.dependency_overrides[get_config] = override_config
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def seeded(engine):
    """A suite with one flaky test, one clean test, and one newly failing test."""
    patterns = {
        "tests/booking.py::Booking::test_reschedule": "PFPFPFPF",
        "tests/booking.py::Booking::test_create": "PPPPPPPP",
        "tests/booking.py::Booking::test_cancel": "PPPPPPFF",
    }
    for index in range(8):
        run = TestRun(
            suite_name="admin-e2e",
            started_at=START + timedelta(hours=index),
            finished_at=START + timedelta(hours=index, minutes=5),
            source_format="junit",
            commit_sha=f"sha{index:04d}",
            branch="main",
            environment="chrome-ci",
            results=[
                TestResult(
                    test_id=test_id,
                    display_name=test_id.rsplit("::", 1)[-1],
                    status=STATUSES[pattern[index]],
                    duration_ms=100 + index * 10,
                    raw_status=pattern[index],
                    failure_message="boom" if pattern[index] == "F" else None,
                    attachments=["shot.png"] if pattern[index] == "F" else [],
                )
                for test_id, pattern in patterns.items()
            ],
        )
        with session_scope(engine) as session:
            store_run(session, run)
    return patterns
