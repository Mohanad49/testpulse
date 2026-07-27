"""Persistence for parsed runs.

The one place where domain dataclasses become rows. Everything above this module
works in dataclasses; everything below works in SQLAlchemy entities.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from testpulse_core.models import TestResult, TestRun
from testpulse_core.storage.schema import TestResultRow, TestRunRow


class DuplicateRunError(Exception):
    """A run with the same natural key is already stored.

    Raised rather than silently skipped. An ingest that quietly does nothing is
    indistinguishable at the call site from one that worked, and in CI that
    difference decides whether a missing dashboard entry is a pipeline bug or
    expected behaviour.
    """

    def __init__(self, existing_id: int, suite_name: str, commit_sha: str | None) -> None:
        self.existing_id = existing_id
        super().__init__(
            f"Run already ingested as id={existing_id} "
            f"(suite={suite_name!r}, commit={commit_sha!r}, same start time). "
            "Re-ingesting a corrected artifact? Use replace=True. A genuine "
            "re-run has a different start time and is not a duplicate."
        )


@dataclass(frozen=True, slots=True)
class IngestSummary:
    """What an ingest actually wrote, for the CLI to report."""

    run_id: int
    suite_name: str
    results_written: int
    replaced_id: int | None = None


def find_existing_run(session: Session, run: TestRun) -> TestRunRow | None:
    """Look up a stored run matching this one's natural key.

    The key is (suite_name, commit_sha, environment, started_at) — the same
    tuple the unique constraint enforces. This check exists so the caller gets a
    typed error with the existing id rather than a raw IntegrityError, but the
    constraint is what actually guarantees the invariant: two concurrent CI jobs
    could both pass this check, and only the database can arbitrate that.
    """
    statement = select(TestRunRow).where(
        TestRunRow.suite_name == run.suite_name,
        TestRunRow.commit_sha == run.commit_sha,
        TestRunRow.environment == run.environment,
        TestRunRow.started_at == run.started_at,
    )
    return session.execute(statement).scalar_one_or_none()


def _to_result_row(result: TestResult) -> TestResultRow:
    return TestResultRow(
        test_id=result.test_id,
        display_name=result.display_name,
        status=str(result.status),
        raw_status=result.raw_status,
        duration_ms=result.duration_ms,
        file_path=result.file_path,
        class_name=result.class_name,
        test_name=result.test_name,
        failure_message=result.failure_message,
        failure_stack=result.failure_stack,
        retry_count=result.retry_count,
        attachments="\n".join(result.attachments) if result.attachments else None,
    )


def store_run(
    session: Session,
    run: TestRun,
    *,
    replace: bool = False,
) -> IngestSummary:
    """Persist a parsed run and its results as one transaction.

    Raises :class:`DuplicateRunError` when the natural key already exists.
    ``replace=True`` deletes the stored run first, which is the operation an
    operator actually wants when re-ingesting a corrected or re-uploaded
    artifact — storing a second copy would double every metric computed over it.

    Note what is *not* a duplicate: a suite genuinely re-run against the same
    commit produces a different ``started_at`` and is stored as its own run.
    That matters, because Phase 2's high-precision flake strategy is built on
    exactly those repeated same-commit runs and would have nothing to read if
    they were collapsed.

    Known weakness: when a report carries no timestamps the parser falls back to
    the ingest time, so every ingest of that artifact gets a distinct
    ``started_at`` and this check never fires. Formats without timestamps
    therefore have no duplicate protection. Passing ``--commit`` does not fix
    it; only the producer emitting timestamps does.
    """
    existing = find_existing_run(session, run)
    replaced_id: int | None = None
    if existing is not None:
        if not replace:
            raise DuplicateRunError(existing.id, run.suite_name, run.commit_sha)
        replaced_id = existing.id
        # Cascade removes the child results; the delete and the insert share one
        # transaction, so a failure here leaves the original run intact.
        session.delete(existing)
        session.flush()

    row = TestRunRow(
        suite_name=run.suite_name,
        commit_sha=run.commit_sha,
        branch=run.branch,
        ci_run_url=run.ci_run_url,
        environment=run.environment,
        source_format=run.source_format,
        started_at=run.started_at,
        finished_at=run.finished_at,
        total=run.total,
        passed=run.passed,
        failed=run.failed,
        skipped=run.skipped,
        errored=run.errored,
        results=[_to_result_row(r) for r in run.results],
    )
    session.add(row)
    session.flush()  # assigns row.id without ending the transaction

    return IngestSummary(
        run_id=row.id,
        suite_name=row.suite_name,
        results_written=len(run.results),
        replaced_id=replaced_id,
    )
