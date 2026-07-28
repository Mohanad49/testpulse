"""Tests for the seam between stored rows and the metrics engine.

The engine is tested on synthetic observations elsewhere. What matters here is
that the right rows get loaded: the window is the right size, the ordering is
right, branches do not bleed into each other, and a test that vanished from the
suite does not quietly become a failure.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from conftest import FIXTURES
from testpulse_core.config import FlakeConfig, NewlyFailingConfig
from testpulse_core.models import RunMetadata, TestResult, TestRun, TestStatus
from testpulse_core.parsers.junit import JUnitParser
from testpulse_core.storage.db import create_db_engine, session_scope
from testpulse_core.storage.queries import (
    first_seen_by_test,
    list_suites,
    observations_by_test,
    suite_metrics,
    window_run_ids,
)
from testpulse_core.storage.repository import store_run
from testpulse_core.storage.schema import Base

START = datetime(2026, 7, 1, tzinfo=UTC)
FLAKE = FlakeConfig()
NEWLY = NewlyFailingConfig()


@pytest.fixture
def engine(tmp_path):
    engine = create_db_engine(f"sqlite:///{tmp_path / 'q.db'}")
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


STATUSES = {
    "P": TestStatus.PASSED,
    "F": TestStatus.FAILED,
    "E": TestStatus.ERROR,
    "S": TestStatus.SKIPPED,
}


def store_history(
    engine,
    patterns: dict[str, str] | str,
    *,
    suite: str = "admin-e2e",
    branch: str = "main",
    commits: list[str] | None = None,
    retries: list[int | None] | None = None,
    durations: list[int] | None = None,
    starting_at: datetime = START,
    commit_prefix: str = "sha",
) -> None:
    """Write one run per character, with every named test in the same run.

    Takes a mapping of test_id to pattern so several tests share their runs, the
    way a real suite does. Writing each test into its own run would collide on
    the ingest natural key, and would model something that never happens.
    """
    if isinstance(patterns, str):
        patterns = {"tests/a.py::Cls::test_one": patterns}
    length = max(len(p) for p in patterns.values())

    for index in range(length):
        results = [
            TestResult(
                test_id=test_id,
                display_name=test_id.rsplit("::", 1)[-1],
                status=STATUSES[pattern[index]],
                duration_ms=durations[index] if durations else 100,
                raw_status=pattern[index],
                retry_count=retries[index] if retries else None,
            )
            for test_id, pattern in patterns.items()
            if index < len(pattern)
        ]
        run = TestRun(
            suite_name=suite,
            started_at=starting_at + timedelta(hours=index),
            finished_at=starting_at + timedelta(hours=index, minutes=1),
            source_format="junit",
            commit_sha=commits[index] if commits else f"{commit_prefix}{index:04d}",
            branch=branch,
            environment="ci",
            results=results,
        )
        with session_scope(engine) as session:
            store_run(session, run)


def test_lists_suites_that_have_runs(engine):
    store_history(engine, "P", suite="alpha")
    store_history(engine, "P", suite="beta")
    with session_scope(engine) as session:
        assert list_suites(session) == ["alpha", "beta"]


def test_window_takes_the_most_recent_runs_and_returns_them_oldest_first(engine):
    store_history(engine, "P" * 10)
    with session_scope(engine) as session:
        ids = window_run_ids(session, "admin-e2e", window_size=3)
    assert len(ids) == 3
    assert ids == sorted(ids), "observations must arrive in chronological order"
    # The three newest runs, not the three oldest.
    assert ids == [8, 9, 10]


def test_window_is_counted_in_runs_not_days(engine):
    # A suite that runs 40 times on Monday and once on Saturday has very
    # different evidence in a 7-day window. Every threshold is a fraction of
    # runs, so the denominator has to be runs.
    store_history(engine, "P" * 60)
    with session_scope(engine) as session:
        assert len(window_run_ids(session, "admin-e2e", window_size=50)) == 50


def test_branch_filter_keeps_a_feature_branch_out_of_main_history(engine):
    # Otherwise a broken branch imports failures that were never main's problem
    # and the flake numbers describe a branch nobody runs.
    store_history(engine, "PPPP", branch="main", commit_prefix="main")
    store_history(
        engine,
        "FFFF",
        branch="feature/x",
        commit_prefix="feat",
        starting_at=START + timedelta(days=1),
    )

    with session_scope(engine) as session:
        metrics = suite_metrics(session, "admin-e2e", FLAKE, NEWLY, branch="main")
    assert metrics[0].pass_rate == 1.0

    with session_scope(engine) as session:
        everything = suite_metrics(session, "admin-e2e", FLAKE, NEWLY)
    assert everything[0].pass_rate == 0.5


def test_first_seen_is_not_windowed(engine):
    # Answering "when did this first appear" from the window would report every
    # long-standing test as new once the window rolls past its origin.
    store_history(engine, "P" * 10)
    with session_scope(engine) as session:
        seen = first_seen_by_test(session, "admin-e2e")
        windowed = suite_metrics(
            session, "admin-e2e", FlakeConfig(window_size=2), NEWLY
        )
    assert seen["tests/a.py::Cls::test_one"].replace(tzinfo=UTC) == START
    assert windowed[0].runs_in_window == 2
    assert windowed[0].first_seen_at.replace(tzinfo=UTC) == START


def test_observations_carry_the_commit_and_retry_context(engine):
    store_history(engine, "PP", commits=["abc", "abc"], retries=[0, 1])
    with session_scope(engine) as session:
        ids = window_run_ids(session, "admin-e2e", 50)
        grouped, names = observations_by_test(session, ids)

    observations = grouped["tests/a.py::Cls::test_one"]
    assert [o.commit_sha for o in observations] == ["abc", "abc"]
    assert [o.retry_count for o in observations] == [0, 1]
    assert names["tests/a.py::Cls::test_one"] == "test_one"


def test_a_test_missing_from_recent_runs_is_absent_not_failing(engine):
    # A deleted or renamed test stops appearing. It must not be reported as
    # failing, and its window must shrink to the runs it was actually in.
    # "gone" stops appearing after run 3; "stayed" continues to run 6.
    store_history(
        engine,
        {"tests/a.py::Cls::gone": "PPP", "tests/a.py::Cls::stayed": "PPPPPP"},
    )

    with session_scope(engine) as session:
        metrics = {m.test_id: m for m in suite_metrics(session, "admin-e2e", FLAKE, NEWLY)}

    gone = metrics["tests/a.py::Cls::gone"]
    assert gone.runs_in_window == 3
    assert gone.pass_rate == 1.0
    assert gone.consecutive_failures == 0
    assert gone.is_flaky is False


def test_same_commit_flake_is_detected_from_stored_data(engine):
    # Two runs of one commit that disagree. Strategy A end to end, through the
    # database rather than on synthetic observations.
    store_history(engine, "PF", commits=["deadbeef", "deadbeef"])
    with session_scope(engine) as session:
        metrics = suite_metrics(session, "admin-e2e", FLAKE, NEWLY)
    assert metrics[0].is_flaky is True
    assert metrics[0].flake_evidence == ("same-commit",)


def test_retry_to_green_alone_marks_a_test_flaky(engine):
    store_history(engine, "P", retries=[2])
    with session_scope(engine) as session:
        metrics = suite_metrics(session, "admin-e2e", FLAKE, NEWLY)
    assert metrics[0].is_flaky is True
    assert metrics[0].flake_evidence == ("same-commit",)


def test_results_are_sorted_flakiest_first(engine):
    store_history(
        engine,
        {
            "tests/a.py::Cls::unstable": "PFPFPFPF",
            "tests/a.py::Cls::solid": "PPPPPPPP",
        },
    )
    with session_scope(engine) as session:
        metrics = suite_metrics(session, "admin-e2e", FLAKE, NEWLY)
    assert metrics[0].test_id.endswith("unstable")
    assert metrics[-1].test_id.endswith("solid")


def test_empty_suite_returns_nothing_rather_than_raising(engine):
    with session_scope(engine) as session:
        assert suite_metrics(session, "never-ran", FLAKE, NEWLY) == []


def test_metrics_over_a_real_ingested_report(engine):
    # The whole path: parse a real pytest JUnit report, store it, compute.
    parsed = JUnitParser().parse(
        FIXTURES / "junit" / "pytest-suite.xml",
        RunMetadata(suite_name="sample", commit_sha="a" * 40, environment="local"),
    )
    with session_scope(engine) as session:
        store_run(session, parsed)

    with session_scope(engine) as session:
        metrics = suite_metrics(session, "sample", FLAKE, NEWLY)

    assert len(metrics) == 9
    by_name = {m.display_name: m for m in metrics}
    assert by_name["test_login_succeeds"].pass_rate == 1.0
    assert by_name["test_cart_total_is_correct"].pass_rate == 0.0
    # One run is not enough evidence for anything.
    assert all(m.is_flaky is False for m in metrics)
    assert all(m.is_newly_failing is False for m in metrics)
    # The skipped test has no scored runs at all.
    assert by_name["test_export_to_csv"].pass_rate is None
    assert by_name["test_export_to_csv"].scored_runs == 0


def test_a_zero_score_flaky_test_still_sorts_above_clean_tests(engine):
    # A same-commit finding can score exactly 0.00: one run, retried, went green,
    # so pass rate is 100% and flip rate is 0. Sorting on score alone buried the
    # most conclusively flaky test in the suite below tests with nothing against
    # them.
    # Built inline rather than through store_history, because the two tests need
    # different retry counts inside the same run and the helper applies one value
    # to every test it writes.
    run = TestRun(
        suite_name="admin-e2e",
        started_at=START,
        finished_at=START + timedelta(minutes=1),
        source_format="playwright",
        commit_sha="cafe1234",
        branch="main",
        environment="ci",
        results=[
            TestResult(
                test_id="tests/a.py::Cls::retried",
                display_name="retried",
                status=TestStatus.PASSED,
                duration_ms=100,
                raw_status="passed",
                retry_count=2,
            ),
            TestResult(
                test_id="tests/a.py::Cls::clean",
                display_name="clean",
                status=TestStatus.PASSED,
                duration_ms=100,
                raw_status="passed",
                retry_count=0,
            ),
        ],
    )
    with session_scope(engine) as session:
        store_run(session, run)
    with session_scope(engine) as session:
        metrics = suite_metrics(session, "admin-e2e", FLAKE, NEWLY)

    assert metrics[0].test_id.endswith("retried")
    assert metrics[0].is_flaky is True
    assert metrics[0].flakiness_score == 0.0
    assert metrics[1].is_flaky is False
