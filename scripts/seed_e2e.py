"""Seed a database for the E2E suite.

Uses the committed fixtures, not generated data, so the E2E assertions can be
exact. A test that asserts "some rows are present" passes on a broken page; a
test that asserts "these rows are present" does not.

The multi-run history is built by ingesting the same real Playwright report
several times under different commits, with a status flipped on one test so the
flake classifier has something true to find. That flip is deliberately visible
here rather than hidden in a fixture, because a reader should be able to see
exactly which data the dashboard is showing and why.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from testpulse_core.models import RunMetadata, TestResult, TestRun, TestStatus
from testpulse_core.parsers.playwright_json import PlaywrightJsonParser
from testpulse_core.storage.db import create_db_engine, session_scope
from testpulse_core.storage.repository import store_run
from testpulse_core.storage.schema import Base

SUITE = "e2e-demo"
FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "packages/testpulse-core/tests/fixtures/playwright/playwright-report.json"
)
START = datetime(2026, 7, 1, 9, 0, tzinfo=UTC)

# Alternating so rolling-flip has a genuine pattern to detect: pass rate 0.5 and
# a flip on every run, which is what a real flaky test looks like.
FLAKY_PATTERN = "PFPFPFPF"


def seed(database_url: str) -> None:
    engine = create_db_engine(database_url)
    Base.metadata.create_all(engine)

    template = PlaywrightJsonParser().parse(FIXTURE, RunMetadata(suite_name=SUITE))
    flaky_id = next(r.test_id for r in template.results if "reschedules" in r.test_id)

    for index, mark in enumerate(FLAKY_PATTERN):
        results = [
            TestResult(
                test_id=r.test_id,
                display_name=r.display_name,
                status=(
                    (TestStatus.PASSED if mark == "P" else TestStatus.FAILED)
                    if r.test_id == flaky_id
                    else r.status
                ),
                duration_ms=r.duration_ms + index * 25,
                raw_status=r.raw_status,
                file_path=r.file_path,
                class_name=r.class_name,
                test_name=r.test_name,
                failure_message=r.failure_message,
                failure_stack=r.failure_stack,
                retry_count=r.retry_count,
                attachments=list(r.attachments),
            )
            for r in template.results
        ]
        run = TestRun(
            suite_name=SUITE,
            started_at=START + timedelta(hours=index),
            finished_at=START + timedelta(hours=index, minutes=2),
            source_format="playwright",
            commit_sha=f"e2e{index:04d}deadbeef",
            branch="main",
            environment="chromium-ci",
            results=results,
        )
        with session_scope(engine) as session:
            store_run(session, run)

    engine.dispose()
    print(f"Seeded {len(FLAKY_PATTERN)} runs into {SUITE}")


if __name__ == "__main__":
    seed(sys.argv[1] if len(sys.argv) > 1 else "sqlite:///e2e.db")
