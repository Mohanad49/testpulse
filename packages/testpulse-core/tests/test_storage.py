"""Storage and ingest tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from conftest import FIXTURES
from testpulse_core.models import RunMetadata, TestResult, TestRun, TestStatus
from testpulse_core.parsers.junit import JUnitParser
from testpulse_core.storage.db import create_db_engine, session_scope
from testpulse_core.storage.repository import (
    DuplicateRunError,
    store_run,
)
from testpulse_core.storage.schema import Base, TestResultRow, TestRunRow

START = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)


@pytest.fixture
def engine(tmp_path):
    engine = create_db_engine(f"sqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


def make_run(**overrides) -> TestRun:
    defaults = dict(
        suite_name="admin-portal-e2e",
        started_at=START,
        finished_at=START + timedelta(seconds=30),
        source_format="junit",
        commit_sha="a" * 40,
        branch="main",
        environment="chrome-ci",
        results=[
            TestResult(
                test_id="tests/a.py::None::test_one",
                display_name="test_one",
                status=TestStatus.PASSED,
                duration_ms=120,
                raw_status="passed",
                retry_count=0,
                attachments=["shot.png", "trace.zip"],
            ),
            TestResult(
                test_id="tests/a.py::None::test_two",
                display_name="test_two",
                status=TestStatus.FAILED,
                duration_ms=900,
                raw_status="failure",
                failure_message="boom",
                retry_count=None,
            ),
        ],
    )
    return TestRun(**{**defaults, **overrides})


def test_stores_run_with_its_results(engine):
    with session_scope(engine) as session:
        summary = store_run(session, make_run())
        assert summary.results_written == 2

    with session_scope(engine) as session:
        row = session.execute(select(TestRunRow)).scalar_one()
        assert row.suite_name == "admin-portal-e2e"
        assert len(row.results) == 2


def test_denormalised_counts_match_the_results(engine):
    with session_scope(engine) as session:
        store_run(session, make_run())
    with session_scope(engine) as session:
        row = session.execute(select(TestRunRow)).scalar_one()
        assert (row.total, row.passed, row.failed, row.skipped, row.errored) == (2, 1, 1, 0, 0)


def test_reingesting_the_same_run_is_rejected(engine):
    with session_scope(engine) as session:
        store_run(session, make_run())
    with pytest.raises(DuplicateRunError) as excinfo, session_scope(engine) as session:
        store_run(session, make_run())
    assert excinfo.value.existing_id == 1
    # The message must point at the fix, not just state the problem.
    assert "replace=True" in str(excinfo.value)


def test_rejected_duplicate_writes_nothing(engine):
    with session_scope(engine) as session:
        store_run(session, make_run())
    with pytest.raises(DuplicateRunError), session_scope(engine) as session:
        store_run(session, make_run())
    with session_scope(engine) as session:
        assert len(session.execute(select(TestRunRow)).scalars().all()) == 1
        assert len(session.execute(select(TestResultRow)).scalars().all()) == 2


def test_replace_overwrites_rather_than_storing_a_second_copy(engine):
    with session_scope(engine) as session:
        store_run(session, make_run())
    with session_scope(engine) as session:
        summary = store_run(session, make_run(), replace=True)
        assert summary.replaced_id == 1
    with session_scope(engine) as session:
        # One run, not two - a second copy would double every downstream metric.
        assert len(session.execute(select(TestRunRow)).scalars().all()) == 1
        # And no orphaned results from the run that was replaced.
        assert len(session.execute(select(TestResultRow)).scalars().all()) == 2


def test_a_genuine_rerun_is_not_a_duplicate(engine):
    # Same suite, same commit, same environment, different wall-clock start.
    # These are two real runs, and Phase 2's same-commit flake strategy is built
    # on exactly this pair - collapsing them would delete the signal.
    with session_scope(engine) as session:
        store_run(session, make_run())
    with session_scope(engine) as session:
        store_run(session, make_run(started_at=START + timedelta(minutes=5)))
    with session_scope(engine) as session:
        assert len(session.execute(select(TestRunRow)).scalars().all()) == 2


def test_same_suite_and_commit_in_a_different_environment_is_not_a_duplicate(engine):
    # Android and chrome runs of one commit are two legitimate runs.
    with session_scope(engine) as session:
        store_run(session, make_run(environment="chrome-ci"))
    with session_scope(engine) as session:
        store_run(session, make_run(environment="android-emulator"))
    with session_scope(engine) as session:
        assert len(session.execute(select(TestRunRow)).scalars().all()) == 2


def test_the_database_enforces_the_key_independently_of_the_precheck(engine):
    # The Python check gives a typed error; the constraint is what actually
    # guarantees the invariant when two CI jobs race past the check together.
    with session_scope(engine) as session:
        store_run(session, make_run())
    with pytest.raises(IntegrityError), session_scope(engine) as session:
        session.add(
            TestRunRow(
                suite_name="admin-portal-e2e",
                commit_sha="a" * 40,
                environment="chrome-ci",
                started_at=START,
                source_format="junit",
            )
        )


def test_retry_count_distinguishes_zero_from_unknown(engine):
    with session_scope(engine) as session:
        store_run(session, make_run())
    with session_scope(engine) as session:
        rows = {
            r.display_name: r for r in session.execute(select(TestResultRow)).scalars().all()
        }
        assert rows["test_one"].retry_count == 0
        assert rows["test_two"].retry_count is None


def test_attachments_round_trip(engine):
    with session_scope(engine) as session:
        store_run(session, make_run())
    with session_scope(engine) as session:
        row = session.execute(
            select(TestResultRow).where(TestResultRow.display_name == "test_one")
        ).scalar_one()
        assert row.attachments is not None
        assert row.attachments.split("\n") == ["shot.png", "trace.zip"]


def test_sqlite_foreign_keys_are_enforced(engine):
    # Off by default in SQLite, which would let the ON DELETE CASCADE silently
    # do nothing locally while working on Postgres.
    with engine.connect() as connection:
        assert connection.execute(text("PRAGMA foreign_keys")).scalar() == 1


def test_deleting_a_run_removes_its_results(engine):
    with session_scope(engine) as session:
        store_run(session, make_run())
    with session_scope(engine) as session:
        session.delete(session.execute(select(TestRunRow)).scalar_one())
    with session_scope(engine) as session:
        assert session.execute(select(TestResultRow)).scalars().all() == []


def test_a_failed_ingest_leaves_no_partial_run(engine):
    # A half-written run would look to Phase 2 like a suite that stopped
    # mid-execution, manufacturing a false signal in the product's own data.
    run = make_run()
    # NOT NULL on test_results.test_id, violated by the second result only, so
    # the run row and the first result are already in the transaction when it
    # fails. test_id is written through untouched, unlike status, which is
    # str()-ed on the way in and would silently become the string "None".
    run.results[1].test_id = None  # type: ignore[assignment]
    with pytest.raises(IntegrityError), session_scope(engine) as session:
        store_run(session, run)
    with session_scope(engine) as session:
        assert session.execute(select(TestRunRow)).scalars().all() == []
        assert session.execute(select(TestResultRow)).scalars().all() == []


def test_end_to_end_from_a_real_report(engine):
    parsed = JUnitParser().parse(
        FIXTURES / "junit" / "pytest-suite.xml",
        RunMetadata(suite_name="sample", commit_sha="f" * 40, environment="local"),
    )
    with session_scope(engine) as session:
        summary = store_run(session, parsed)
        assert summary.results_written == 9

    with session_scope(engine) as session:
        row = session.execute(select(TestRunRow)).scalar_one()
        assert (row.total, row.passed, row.failed, row.errored, row.skipped) == (9, 5, 2, 1, 1)
        assert row.source_format == "junit"
        statuses = {r.status for r in row.results}
        assert statuses == {"passed", "failed", "skipped", "error"}
