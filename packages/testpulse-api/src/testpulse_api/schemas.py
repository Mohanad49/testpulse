"""Response models.

Written by hand rather than generated from the SQLAlchemy entities. The database
shape and the API shape are allowed to diverge, and they already do: the API
exposes computed metrics that are in no table, and hides columns like the raw
failure stack that would make a list response enormous. Generating these from the
ORM would couple every future schema change to a published contract.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class RunSummarySchema(BaseModel):
    """One run, as a table row or a point on a trend line."""

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


class SuiteSchema(BaseModel):
    name: str


class SuiteHealthSchema(BaseModel):
    """Aggregate health, for the top of a suite overview screen."""

    suite_name: str
    runs_in_window: int
    total_tests: int
    flaky_count: int
    newly_failing_count: int
    pass_rate: float | None = Field(
        description=(
            "Across every scored result in the window, not the mean of per-run "
            "rates. Null when nothing in the window was scored, which is not the "
            "same as zero."
        )
    )
    mean_run_duration_ms: float
    run_duration_trend_ms_per_run: float = Field(
        description="Least-squares slope. Positive means the suite is getting slower."
    )
    recent_runs: list[RunSummarySchema]


class TestMetricsSchema(BaseModel):
    """Computed health for one test."""

    test_id: str
    display_name: str
    runs_in_window: int
    scored_runs: int = Field(
        description="Runs excluding skips. A metric from 3 of 50 runs deserves less trust."
    )
    pass_rate: float | None
    flip_rate: float
    flakiness_score: float = Field(
        description=(
            "Ranking aid, not a probability. Describes the rolling-flip strategy "
            "only, so a same-commit finding can legitimately score 0.0."
        )
    )
    mean_duration_ms: float
    p95_duration_ms: int
    duration_trend_ms_per_run: float
    first_seen_at: datetime
    last_failed_at: datetime | None
    consecutive_failures: int
    is_newly_failing: bool
    is_flaky: bool
    flake_evidence: list[str] = Field(
        description="Which strategies fired: 'same-commit', 'rolling-flip', or both."
    )
    is_quarantined: bool


class PagedTestsSchema(BaseModel):
    """A page of tests plus what it took to build it.

    ``total`` is the count before slicing, so a client can render a pager without
    a second request.
    """

    suite_name: str
    total: int
    limit: int
    offset: int
    sort_by: str
    order: str
    items: list[TestMetricsSchema]


class TimelinePointSchema(BaseModel):
    """One cell of a test's status timeline.

    Carries the run's commit and time because a strip of coloured cells is
    unreadable without knowing which commit each cell belongs to.
    """

    run_id: int
    started_at: datetime
    commit_sha: str | None
    branch: str | None
    status: str
    raw_status: str
    duration_ms: int
    retry_count: int | None
    failure_message: str | None


class TestDetailSchema(BaseModel):
    metrics: TestMetricsSchema
    timeline: list[TimelinePointSchema]
    attachments: list[str] = Field(
        description="Attachment references from the most recent appearance of this test."
    )


class FailureClusterSchema(BaseModel):
    """A group of failures sharing one root cause."""

    template: str = Field(
        description="Normalised form: the shape of the problem with the varying parts removed."
    )
    count: int
    representative: str = Field(
        description="One real, unedited message. The template alone is not debuggable."
    )
    test_ids: list[str] = Field(
        description="A cluster spanning many tests points at shared infrastructure."
    )


class QuarantineEntrySchema(BaseModel):
    suite_name: str
    test_id: str
    quarantined_at: datetime
    expires_at: datetime
    expires_after_days: int
    days_remaining: int = Field(
        description="Negative once expired, so a client can say how far overdue an entry is."
    )
    is_expired: bool
    reason: str | None
    quarantined_by: str | None


class QuarantineListSchema(BaseModel):
    suite_name: str
    entries: list[QuarantineEntrySchema]
    debt_count: int = Field(
        description="Entries past their expiry. The number a team should be shown."
    )


class IngestResponseSchema(BaseModel):
    run_id: int
    suite_name: str
    results_written: int
    replaced_id: int | None
    warnings: list[str]


class ErrorSchema(BaseModel):
    """Every non-2xx response body.

    One shape for all of them so a client has exactly one error path to write.
    """

    detail: str
