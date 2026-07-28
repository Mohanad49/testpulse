"""Suite-scoped read endpoints."""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Path, Query, status
from testpulse_core import quarantine as quarantine_service
from testpulse_core.metrics import TestMetrics
from testpulse_core.storage import queries

from testpulse_api.deps import ConfigDep, SessionDep
from testpulse_api.schemas import (
    PagedTestsSchema,
    QuarantineEntrySchema,
    QuarantineListSchema,
    RunSummarySchema,
    SuiteHealthSchema,
    SuiteSchema,
    TestDetailSchema,
    TestMetricsSchema,
    TimelinePointSchema,
)

router = APIRouter(prefix="/api", tags=["suites"])

# Sorting is restricted to a fixed set of names rather than reflecting whatever
# the caller sends. Passing user input to getattr on a dataclass would let a
# request sort by any attribute that happens to exist, which is both a way to
# leak the internal shape of the object and a way to crash the endpoint with a
# 500 on a typo. An allowlist turns both into a 422.
SORTABLE: dict[str, str] = {
    "flakiness_score": "flakiness_score",
    "pass_rate": "pass_rate",
    "flip_rate": "flip_rate",
    "p95_duration_ms": "p95_duration_ms",
    "mean_duration_ms": "mean_duration_ms",
    "duration_trend_ms_per_run": "duration_trend_ms_per_run",
    "consecutive_failures": "consecutive_failures",
    "runs_in_window": "runs_in_window",
    "first_seen_at": "first_seen_at",
    "display_name": "display_name",
}

SuiteName = Annotated[str, Path(description="Suite name as stored at ingest time.")]


def _to_metrics_schema(metric: TestMetrics, quarantined: set[str]) -> TestMetricsSchema:
    return TestMetricsSchema(
        test_id=metric.test_id,
        display_name=metric.display_name,
        runs_in_window=metric.runs_in_window,
        scored_runs=metric.scored_runs,
        pass_rate=metric.pass_rate,
        flip_rate=metric.flip_rate,
        flakiness_score=metric.flakiness_score,
        mean_duration_ms=metric.mean_duration_ms,
        p95_duration_ms=metric.p95_duration_ms,
        duration_trend_ms_per_run=metric.duration_trend_ms_per_run,
        first_seen_at=metric.first_seen_at,
        last_failed_at=metric.last_failed_at,
        consecutive_failures=metric.consecutive_failures,
        is_newly_failing=metric.is_newly_failing,
        is_flaky=metric.is_flaky,
        flake_evidence=list(metric.flake_evidence),
        is_quarantined=metric.test_id in quarantined,
    )


def _quarantined_ids(session: SessionDep, suite: str) -> set[str]:
    return {entry.test_id for entry in quarantine_service.list_entries(session, suite)}


def _require_suite(session: SessionDep, suite: str) -> None:
    if suite not in queries.list_suites(session):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No runs stored for suite {suite!r}.",
        )


@router.get("/suites", response_model=list[SuiteSchema])
def get_suites(session: SessionDep) -> list[SuiteSchema]:
    """Every suite that has at least one stored run."""
    return [SuiteSchema(name=name) for name in queries.list_suites(session)]


@router.get("/suites/{suite}/runs", response_model=list[RunSummarySchema])
def get_runs(
    suite: SuiteName,
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    branch: str | None = None,
) -> list[RunSummarySchema]:
    """Recent runs, newest first.

    ``limit`` is capped rather than unbounded. An uncapped limit on a suite with
    years of history is a way to make the server assemble an arbitrarily large
    response on request.
    """
    _require_suite(session, suite)
    runs = queries.recent_runs(session, suite, limit=limit, branch=branch)
    return [RunSummarySchema.model_validate(run, from_attributes=True) for run in runs]


@router.get("/suites/{suite}/health", response_model=SuiteHealthSchema)
def get_health(
    suite: SuiteName,
    session: SessionDep,
    config: ConfigDep,
    branch: str | None = None,
) -> SuiteHealthSchema:
    """Aggregate health over the configured window."""
    health = queries.suite_health(
        session, suite, config.flake, config.newly_failing, branch=branch
    )
    if health is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No runs stored for suite {suite!r}.",
        )
    return SuiteHealthSchema(
        suite_name=health.suite_name,
        runs_in_window=health.runs_in_window,
        total_tests=health.total_tests,
        flaky_count=health.flaky_count,
        newly_failing_count=health.newly_failing_count,
        pass_rate=health.pass_rate,
        mean_run_duration_ms=health.mean_run_duration_ms,
        run_duration_trend_ms_per_run=health.run_duration_trend_ms_per_run,
        recent_runs=[
            RunSummarySchema.model_validate(run, from_attributes=True)
            for run in health.recent_runs
        ],
    )


@router.get("/suites/{suite}/tests", response_model=PagedTestsSchema)
def get_tests(
    suite: SuiteName,
    session: SessionDep,
    config: ConfigDep,
    sort_by: Annotated[str, Query(description=f"One of: {', '.join(sorted(SORTABLE))}")] = (
        "flakiness_score"
    ),
    order: Literal["asc", "desc"] = "desc",
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    branch: str | None = None,
) -> PagedTestsSchema:
    """Every test in the window, sortable and paginated.

    Known scaling limit, stated rather than hidden: metrics for the whole suite
    are computed in Python on every request and then sliced. Sorting by a
    computed metric cannot be pushed into SQL because the metrics are not stored,
    so paging does not reduce the work done. Fine at portfolio scale, and the fix
    when it stops being fine is a materialised metrics table refreshed on ingest,
    not a cleverer query.
    """
    _require_suite(session, suite)
    if sort_by not in SORTABLE:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Cannot sort by {sort_by!r}. Allowed: {', '.join(sorted(SORTABLE))}.",
        )

    metrics = queries.suite_metrics(
        session, suite, config.flake, config.newly_failing, branch=branch
    )
    quarantined = _quarantined_ids(session, suite)

    def sort_key(metric: TestMetrics) -> tuple[int, float | str]:
        value = getattr(metric, SORTABLE[sort_by])
        # None sorts last in both directions. A test with no pass rate has not
        # scored well or badly, and letting it float to the top of an ascending
        # sort would put "no data" above "worst".
        if value is None:
            return (1, 0.0)
        if isinstance(value, str):
            return (0, value)
        if hasattr(value, "timestamp"):
            return (0, value.timestamp())
        return (0, float(value))

    ordered = sorted(metrics, key=sort_key, reverse=(order == "desc"))
    page = ordered[offset : offset + limit]

    return PagedTestsSchema(
        suite_name=suite,
        total=len(ordered),
        limit=limit,
        offset=offset,
        sort_by=sort_by,
        order=order,
        items=[_to_metrics_schema(m, quarantined) for m in page],
    )


@router.get("/suites/{suite}/flaky", response_model=list[TestMetricsSchema])
def get_flaky(
    suite: SuiteName,
    session: SessionDep,
    config: ConfigDep,
    branch: str | None = None,
) -> list[TestMetricsSchema]:
    """Tests the classifier considers flaky, with the evidence that fired."""
    _require_suite(session, suite)
    metrics = queries.suite_metrics(
        session, suite, config.flake, config.newly_failing, branch=branch
    )
    quarantined = _quarantined_ids(session, suite)
    return [_to_metrics_schema(m, quarantined) for m in metrics if m.is_flaky]


@router.get("/suites/{suite}/quarantine", response_model=QuarantineListSchema)
def get_quarantine(suite: SuiteName, session: SessionDep) -> QuarantineListSchema:
    """Quarantined tests, most overdue first."""
    entries = quarantine_service.list_entries(session, suite)
    return QuarantineListSchema(
        suite_name=suite,
        entries=[
            QuarantineEntrySchema(
                suite_name=entry.suite_name,
                test_id=entry.test_id,
                quarantined_at=entry.quarantined_at,
                expires_at=entry.expires_at,
                expires_after_days=entry.expires_after_days,
                days_remaining=entry.days_remaining,
                is_expired=entry.is_expired,
                reason=entry.reason,
                quarantined_by=entry.quarantined_by,
            )
            for entry in entries
        ],
        debt_count=len(quarantine_service.debt(entries)),
    )


@router.get("/suites/{suite}/tests/{test_id:path}", response_model=TestDetailSchema)
def get_test_detail(
    suite: SuiteName,
    test_id: Annotated[str, Path(description="Full test_id. May contain slashes.")],
    session: SessionDep,
    config: ConfigDep,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> TestDetailSchema:
    """One test's metrics plus its full status timeline.

    Two deliberate divergences from the original endpoint sketch, which had this
    as a flat ``/api/tests/{test_id}``.

    First, it is scoped under a suite. ``test_id`` is not globally unique:
    ``tests/a.py::Cls::test_login`` can exist in an admin suite and a mobile
    suite at once, and a flat route would silently merge two different tests'
    histories into one chart.

    Second, the path uses a ``:path`` converter because ``test_id`` genuinely
    contains slashes (``recruitment/recruitment.spec.ts::...``). The alternative
    was encoding the id, and percent-encoded slashes are unreliable: some proxies
    normalise ``%2F`` back to ``/`` before the app ever sees it, so the encoding
    silently stops working in exactly the deployment where it matters.
    """
    _require_suite(session, suite)
    history = queries.test_history(session, suite, test_id, limit=limit)
    if not history:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No test {test_id!r} recorded in suite {suite!r}.",
        )

    metrics = queries.suite_metrics(session, suite, config.flake, config.newly_failing)
    match = next((m for m in metrics if m.test_id == test_id), None)
    if match is None:
        # The test exists in history but has fallen out of the current window.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"Test {test_id!r} exists in {suite!r} but has no runs inside the "
                f"current window of {config.flake.window_size} runs."
            ),
        )

    quarantined = _quarantined_ids(session, suite)
    latest_result = history[-1][1]

    return TestDetailSchema(
        metrics=_to_metrics_schema(match, quarantined),
        timeline=[
            TimelinePointSchema(
                run_id=run.id,
                started_at=run.started_at,
                commit_sha=run.commit_sha,
                branch=run.branch,
                status=result.status,
                raw_status=result.raw_status,
                duration_ms=result.duration_ms,
                retry_count=result.retry_count,
                failure_message=result.failure_message,
            )
            for run, result in history
        ],
        attachments=(latest_result.attachments or "").split("\n")
        if latest_result.attachments
        else [],
    )
