"""Reading history back out for the metrics engine.

The engine itself is pure. This module is the seam where stored rows become
observations, and it is the only place that knows a window is "the last N runs of
a suite" rather than "the last N days" or "everything since the last release".
"""

from __future__ import annotations

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
