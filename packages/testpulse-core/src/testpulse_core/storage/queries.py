"""Reading history back out for the metrics engine.

The engine itself is pure. This module is the seam where stored rows become
observations, and it is the only place that knows a window is "the last N runs of
a suite" rather than "the last N days" or "everything since the last release".
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from testpulse_core.config import FlakeConfig, NewlyFailingConfig
from testpulse_core.metrics import Observation, TestMetrics, compute
from testpulse_core.models import TestStatus
from testpulse_core.storage.schema import TestResultRow, TestRunRow


def list_suites(session: Session) -> list[str]:
    statement = select(TestRunRow.suite_name).distinct().order_by(TestRunRow.suite_name)
    return list(session.execute(statement).scalars().all())


def window_run_ids(
    session: Session,
    suite_name: str,
    window_size: int,
    branch: str | None = None,
) -> list[int]:
    """The most recent ``window_size`` run ids for a suite, oldest first.

    The window is counted in runs rather than in days on purpose. A suite that
    runs 40 times on Monday and once on Saturday has wildly different amounts of
    evidence in a seven-day window, and every threshold in the flake config is
    expressed as a fraction of runs. Counting runs keeps the denominator
    meaningful.

    Filtering by branch is optional and usually wanted. Mixing a long-lived
    feature branch into main's history imports failures that were never main's
    problem, and the flake numbers end up describing a branch nobody is running.
    """
    statement = (
        select(TestRunRow.id)
        .where(TestRunRow.suite_name == suite_name)
        .order_by(TestRunRow.started_at.desc())
        .limit(window_size)
    )
    if branch is not None:
        statement = statement.where(TestRunRow.branch == branch)
    newest_first = list(session.execute(statement).scalars().all())
    return list(reversed(newest_first))


def first_seen_by_test(session: Session, suite_name: str) -> dict[str, datetime]:
    """Earliest run per test, across all history rather than just the window.

    Deliberately not windowed. "When did this test first appear" is a question
    about the test's whole life, and answering it from the window would report
    every long-standing test as new the moment the window rolls past its origin.
    """
    statement = (
        select(TestResultRow.test_id, func.min(TestRunRow.started_at))
        .join(TestRunRow, TestRunRow.id == TestResultRow.run_id)
        .where(TestRunRow.suite_name == suite_name)
        .group_by(TestResultRow.test_id)
    )
    return {test_id: seen for test_id, seen in session.execute(statement).all()}


def observations_by_test(
    session: Session,
    run_ids: list[int],
) -> tuple[dict[str, list[Observation]], dict[str, str]]:
    """Load every result in the window, grouped by test.

    Returns the observations and a display name per test. The display name comes
    from the most recent result rather than the oldest, so a renamed test reads
    with its current name even while its history is keyed on the old id.
    """
    if not run_ids:
        return {}, {}

    statement = (
        select(
            TestResultRow.test_id,
            TestResultRow.display_name,
            TestResultRow.status,
            TestResultRow.duration_ms,
            TestResultRow.retry_count,
            TestRunRow.id,
            TestRunRow.started_at,
            TestRunRow.commit_sha,
        )
        .join(TestRunRow, TestRunRow.id == TestResultRow.run_id)
        .where(TestResultRow.run_id.in_(run_ids))
        .order_by(TestRunRow.started_at)
    )

    grouped: dict[str, list[Observation]] = {}
    names: dict[str, str] = {}
    for row in session.execute(statement).all():
        (
            test_id,
            display_name,
            status,
            duration_ms,
            retry_count,
            run_id,
            started_at,
            commit_sha,
        ) = row
        grouped.setdefault(test_id, []).append(
            Observation(
                run_id=run_id,
                started_at=started_at,
                status=TestStatus(status),
                duration_ms=duration_ms,
                commit_sha=commit_sha,
                retry_count=retry_count,
            )
        )
        # Ordered oldest first, so the last write wins and that is the newest name.
        names[test_id] = display_name

    return grouped, names


def suite_metrics(
    session: Session,
    suite_name: str,
    flake_config: FlakeConfig,
    newly_failing_config: NewlyFailingConfig,
    branch: str | None = None,
) -> list[TestMetrics]:
    """Compute metrics for every test in a suite's current window."""
    run_ids = window_run_ids(session, suite_name, flake_config.window_size, branch)
    grouped, names = observations_by_test(session, run_ids)
    first_seen = first_seen_by_test(session, suite_name)

    results = [
        compute(
            test_id=test_id,
            display_name=names.get(test_id, test_id),
            observations=observations,
            first_seen=first_seen.get(test_id, observations[0].started_at),
            flake_config=flake_config,
            newly_failing_config=newly_failing_config,
        )
        for test_id, observations in grouped.items()
    ]
    # Confirmed flaky first, then by score, then slowest.
    #
    # The is_flaky term is not redundant, and leaving it out was a real bug. A
    # test caught by same-commit evidence can have a flakiness score of exactly
    # 0.00: one run, retried, went green. Pass rate 100%, flip rate 0, score 0.
    # Sorting on score alone put the most conclusively flaky test in the suite at
    # the bottom of the leaderboard, below tests with no evidence against them at
    # all. The score is a Strategy B ranking aid and says nothing about Strategy
    # A, so it cannot be the only sort key.
    results.sort(
        key=lambda m: (not m.is_flaky, -m.flakiness_score, -m.p95_duration_ms)
    )
    return results


@dataclass(frozen=True, slots=True)
class RunSummary:
    """One run, reduced to what a dashboard row or a sparkline point needs."""

    id: int
    started_at: datetime
    finished_at: datetime | None
    commit_sha: str | None
    branch: str | None
    environment: str | None
    source_format: str
    total: int
    passed: int
    failed: int
    skipped: int
    errored: int
    duration_ms: int


@dataclass(frozen=True, slots=True)
class SuiteHealth:
    """Suite-level aggregate over the current window."""

    suite_name: str
    runs_in_window: int
    total_tests: int
    flaky_count: int
    newly_failing_count: int
    pass_rate: float | None
    """Across every scored result in the window, not the mean of per-run rates.
    Averaging per-run rates would weight a 3-test smoke run the same as a
    400-test regression run."""

    mean_run_duration_ms: float
    run_duration_trend_ms_per_run: float
    recent_runs: list[RunSummary]


def recent_runs(
    session: Session,
    suite_name: str,
    limit: int = 20,
    branch: str | None = None,
) -> list[RunSummary]:
    """Most recent runs first, which is the order a runs table displays them in."""
    statement = (
        select(TestRunRow)
        .where(TestRunRow.suite_name == suite_name)
        .order_by(TestRunRow.started_at.desc())
        .limit(limit)
    )
    if branch is not None:
        statement = statement.where(TestRunRow.branch == branch)

    summaries = []
    for row in session.execute(statement).scalars().all():
        # finished_at is an estimate for some formats (see the JUnit parser), so
        # a negative or absent span collapses to zero rather than propagating a
        # nonsense duration into a chart.
        span = 0
        if row.finished_at is not None:
            span = max(int((row.finished_at - row.started_at).total_seconds() * 1000), 0)
        summaries.append(
            RunSummary(
                id=row.id,
                started_at=row.started_at,
                finished_at=row.finished_at,
                commit_sha=row.commit_sha,
                branch=row.branch,
                environment=row.environment,
                source_format=row.source_format,
                total=row.total,
                passed=row.passed,
                failed=row.failed,
                skipped=row.skipped,
                errored=row.errored,
                duration_ms=span,
            )
        )
    return summaries


def suite_health(
    session: Session,
    suite_name: str,
    flake_config: FlakeConfig,
    newly_failing_config: NewlyFailingConfig,
    branch: str | None = None,
    recent_limit: int = 20,
) -> SuiteHealth | None:
    """Aggregate health for a suite, or None if it has no runs at all.

    None rather than a zeroed record: "this suite has never run" and "this suite
    ran and everything failed" are different answers and a caller must be able to
    tell them apart. The API turns the first into a 404.
    """
    runs = recent_runs(session, suite_name, limit=flake_config.window_size, branch=branch)
    if not runs:
        return None

    metrics = suite_metrics(session, suite_name, flake_config, newly_failing_config, branch)

    scored_total = sum(run.passed + run.failed + run.errored for run in runs)
    passed_total = sum(run.passed for run in runs)

    # Runs come back newest first; reverse so the trend slope points forward in
    # time. Getting this backwards silently inverts every trend arrow.
    oldest_first = list(reversed(runs))
    durations = [run.duration_ms for run in oldest_first]
    n = len(durations)
    if n < 2:
        slope = 0.0
    else:
        mean_x = (n - 1) / 2
        mean_y = sum(durations) / n
        denominator = sum((i - mean_x) ** 2 for i in range(n))
        slope = (
            sum((i - mean_x) * (d - mean_y) for i, d in enumerate(durations)) / denominator
            if denominator
            else 0.0
        )

    return SuiteHealth(
        suite_name=suite_name,
        runs_in_window=len(runs),
        total_tests=len(metrics),
        flaky_count=sum(1 for m in metrics if m.is_flaky),
        newly_failing_count=sum(1 for m in metrics if m.is_newly_failing),
        pass_rate=(passed_total / scored_total) if scored_total else None,
        mean_run_duration_ms=(sum(durations) / n) if n else 0.0,
        run_duration_trend_ms_per_run=slope,
        recent_runs=runs[:recent_limit],
    )


def test_history(
    session: Session,
    suite_name: str,
    test_id: str,
    limit: int = 100,
) -> list[tuple[RunSummary, TestResultRow]]:
    """Every recorded appearance of one test, oldest first.

    Returns the run alongside the result because a status timeline is meaningless
    without the commit and time each cell belongs to.
    """
    statement = (
        select(TestRunRow, TestResultRow)
        .join(TestResultRow, TestRunRow.id == TestResultRow.run_id)
        .where(TestRunRow.suite_name == suite_name, TestResultRow.test_id == test_id)
        .order_by(TestRunRow.started_at.desc())
        .limit(limit)
    )
    pairs = []
    for run_row, result_row in session.execute(statement).all():
        span = 0
        if run_row.finished_at is not None:
            span = max(int((run_row.finished_at - run_row.started_at).total_seconds() * 1000), 0)
        pairs.append(
            (
                RunSummary(
                    id=run_row.id,
                    started_at=run_row.started_at,
                    finished_at=run_row.finished_at,
                    commit_sha=run_row.commit_sha,
                    branch=run_row.branch,
                    environment=run_row.environment,
                    source_format=run_row.source_format,
                    total=run_row.total,
                    passed=run_row.passed,
                    failed=run_row.failed,
                    skipped=run_row.skipped,
                    errored=run_row.errored,
                    duration_ms=span,
                ),
                result_row,
            )
        )
    return list(reversed(pairs))
