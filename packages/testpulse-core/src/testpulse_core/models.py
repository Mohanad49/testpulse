"""Storage-independent domain model for a single ingested test run.

Parsers return these dataclasses and nothing else. They never touch a database
session, which is why a parser test needs no fixtures beyond a file on disk.
The mapping from these objects to SQLAlchemy entities lives in
``testpulse_core.storage.repository``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum


class TestStatus(StrEnum):
    """The normalised status vocabulary every parser maps onto.

    Four values, not more. Report formats offer between four and six, but the
    extra ones ("broken", "timedOut", "interrupted") describe *how* a test
    failed rather than *whether* it did, and flake detection in Phase 2 only
    cares about whether. The original vocabulary is preserved verbatim in
    ``TestResult.raw_status`` so nothing is lost, and so a future consumer that
    does care can recover it without re-parsing.
    """

    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"
    ERROR = "error"

    # This project's domain nouns unavoidably start with "Test", which is also
    # pytest's class-collection prefix. Opting out explicitly is clearer than
    # renaming the domain or widening pytest's collection config.
    __test__ = False


@dataclass(frozen=True, slots=True)
class RunMetadata:
    """CI context that report files do not contain and the caller must supply.

    No report format records which commit or branch produced it — that is
    knowledge only the CI job has. Phase 2's strongest flake strategy compares
    runs against the same ``commit_sha``, so this is not optional metadata: an
    ingest without a commit SHA produces data that same-commit flake detection
    can never use.
    """

    suite_name: str
    commit_sha: str | None = None
    branch: str | None = None
    ci_run_url: str | None = None
    environment: str | None = None


@dataclass(slots=True)
class TestResult:
    """One test outcome within one run."""

    __test__ = False

    test_id: str
    display_name: str
    status: TestStatus
    duration_ms: int
    raw_status: str
    file_path: str | None = None
    class_name: str | None = None
    test_name: str | None = None
    failure_message: str | None = None
    failure_stack: str | None = None
    retry_count: int | None = None
    """``None`` means the format does not report retries at all; ``0`` means it
    does and there were none. Collapsing the two would let Phase 2 conclude "no
    retries happened" from a format that structurally cannot say so."""
    attachments: list[str] = field(default_factory=list)


@dataclass(slots=True)
class TestRun:
    """A parsed report file or directory, plus the CI context around it."""

    __test__ = False

    suite_name: str
    started_at: datetime
    finished_at: datetime | None
    results: list[TestResult]
    source_format: str
    commit_sha: str | None = None
    branch: str | None = None
    ci_run_url: str | None = None
    environment: str | None = None

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def passed(self) -> int:
        return sum(1 for r in self.results if r.status is TestStatus.PASSED)

    @property
    def failed(self) -> int:
        return sum(1 for r in self.results if r.status is TestStatus.FAILED)

    @property
    def skipped(self) -> int:
        return sum(1 for r in self.results if r.status is TestStatus.SKIPPED)

    @property
    def errored(self) -> int:
        return sum(1 for r in self.results if r.status is TestStatus.ERROR)


# A trailing ":line" or ":line:col" as Playwright's Allure reporter emits in fullName,
# e.g. "recruitment/recruitment.spec.ts:65:7".
_SOURCE_POSITION = re.compile(r":\d+(?::\d+)?$")


def strip_source_position(value: str) -> str:
    """Remove a trailing line/column reference from a file path.

    Playwright records a test's source position inside its identifier. Inserting
    a line anywhere above a test shifts that number, which would silently mint a
    new ``test_id`` and reset the test's history — on a run where nothing about
    the test itself changed. Stripping the position trades a small loss of
    precision (two tests declared on different lines of one file with the same
    name collapse together) for identity that survives ordinary editing.
    """
    return _SOURCE_POSITION.sub("", value)


def build_test_id(
    file_path: str | None,
    class_name: str | None,
    test_name: str,
) -> str:
    """Compose the stable identifier used to join a test's results across runs.

    Format: ``file_path::class_name::test_name``, with empty segments preserved
    so the shape is fixed and parseable. The components are also stored
    individually on ``TestResult`` so a future version can re-key existing rows
    without re-ingesting the source reports.

    Known limitation, accepted for now: renaming a test, moving its file, or
    renaming its class produces a new identifier, and the old history is
    stranded rather than migrated. Every alternative considered was worse.
    Report-supplied identifiers (Allure's ``historyId``) are computed
    differently by each framework's adapter, so they cannot join results across
    a suite that changes tooling — which is the exact scenario this project was
    built around. Fuzzy name matching would silently merge genuinely distinct
    tests, and a wrong merge is more damaging than a reset history because it
    corrupts the metrics rather than just emptying them.
    """
    return "::".join(
        [
            strip_source_position(file_path) if file_path else "",
            class_name or "",
            test_name,
        ]
    )
