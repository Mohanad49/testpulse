"""pytest-json-report parser.

The only format here that reports a test's lifecycle phases separately: setup,
call and teardown each carry their own outcome and duration. That extra
resolution answers a question the other formats cannot — whether a test is slow
because of the test or because of its fixtures.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from testpulse_core.models import (
    RunMetadata,
    TestResult,
    TestRun,
    TestStatus,
    build_test_id,
)
from testpulse_core.parsers.base import ParseError, register

_STATUS_MAP = {
    "passed": TestStatus.PASSED,
    "failed": TestStatus.FAILED,
    "skipped": TestStatus.SKIPPED,
    "error": TestStatus.ERROR,
    # An expected failure that failed as expected is a pass: the suite asserted
    # a known-broken behaviour and the assertion held.
    "xfailed": TestStatus.PASSED,
    # An expected failure that passed. The test did not fail, but something the
    # suite believed was broken now works, so the xfail marker is stale. Recording
    # it as passed would hide that; error surfaces it as needing attention.
    "xpassed": TestStatus.ERROR,
}

_PHASES = ("setup", "call", "teardown")


def _split_nodeid(nodeid: str) -> tuple[str | None, str | None, str]:
    """Split a pytest nodeid into ``(file, class, name)``.

    Nodeids look like ``tests/test_api.py::TestBooking::test_creates`` with the
    class segment absent for module-level tests. Parametrised names keep their
    ``[...]`` suffix, which is what makes each parameter set a separate test.
    """
    parts = nodeid.split("::")
    if len(parts) == 1:
        return None, None, parts[0]
    file_path = parts[0]
    name = parts[-1]
    class_name = "::".join(parts[1:-1]) or None
    return file_path, class_name, name


def _outcome(test: dict[str, Any]) -> str:
    """Return the outcome that decided this test.

    The top-level ``outcome`` field already accounts for the phases, so it is
    trusted directly. The phases are still consulted for failure detail, because
    a test that errors in setup carries its traceback under ``setup`` and has no
    ``call`` phase at all — reading only ``call`` would lose the message
    entirely, which is the failure mode this function exists to prevent.
    """
    return str(test.get("outcome", "error"))


def _failing_phase(test: dict[str, Any]) -> dict[str, Any] | None:
    """Find the phase that carries the failure detail, whichever one it was."""
    for phase in _PHASES:
        data = test.get(phase)
        if isinstance(data, dict) and data.get("outcome") not in (None, "passed"):
            return data
    return None


def _total_duration_ms(test: dict[str, Any]) -> int:
    """Sum setup, call and teardown into one duration, in milliseconds.

    Charging fixture time to the test is deliberate. From CI's point of view a
    test that takes 200ms to assert and 8s to set up costs 8.2s, and a Phase 3
    "slowest tests" view that reported 200ms would send someone optimising the
    wrong thing. The per-phase split is discarded here rather than lost —
    re-parsing the report recovers it if a later phase wants that breakdown.
    """
    seconds = 0.0
    for phase in _PHASES:
        data = test.get(phase)
        if isinstance(data, dict):
            value = data.get("duration")
            if isinstance(value, int | float):
                seconds += float(value)
    return round(seconds * 1000)


class PytestJsonParser:
    """Parses pytest-json-report output into a normalised run."""

    format_name = "pytest-json"

    def parse(self, path: Path, meta: RunMetadata) -> TestRun:
        if not path.is_file():
            raise ParseError(f"pytest-json-report file not found at {path}")
        try:
            document = json.loads(path.read_text())
        except json.JSONDecodeError as exc:
            raise ParseError(f"{path} is not valid JSON: {exc}") from exc
        if not isinstance(document, dict) or "tests" not in document:
            raise ParseError(
                f"{path} does not look like a pytest-json-report file "
                "(no top-level 'tests' key)"
            )

        results: list[TestResult] = []
        for test in document["tests"]:
            nodeid = test.get("nodeid")
            if not nodeid:
                raise ParseError(f"{path} contains a test entry with no nodeid")
            file_path, class_name, name = _split_nodeid(nodeid)
            raw_status = _outcome(test)
            phase = _failing_phase(test) or {}
            crash = phase.get("crash") or {}
            longrepr = phase.get("longrepr")

            results.append(
                TestResult(
                    test_id=build_test_id(file_path, class_name, name),
                    display_name=name,
                    status=_STATUS_MAP.get(raw_status, TestStatus.ERROR),
                    duration_ms=_total_duration_ms(test),
                    raw_status=raw_status,
                    file_path=file_path,
                    class_name=class_name,
                    test_name=name,
                    failure_message=crash.get("message"),
                    failure_stack=longrepr if isinstance(longrepr, str) else None,
                    # pytest-json-report has no retry concept of its own. Reruns
                    # via pytest-rerunfailures are not represented in the schema,
                    # so this is unknown rather than zero.
                    retry_count=None,
                    attachments=[],
                )
            )

        # `created` is when the report was written, i.e. the END of the session,
        # not the start. The field name reads like a start time and using it as
        # one would shift every run forward by its own duration - invisible on a
        # fast suite, and a whole-run offset on a slow one. Verified against a
        # JUnit report emitted from the same pytest session: created - duration
        # matches that report's suite timestamp to within microseconds.
        created = document.get("created")
        duration = document.get("duration")
        finished_at = (
            datetime.fromtimestamp(float(created), tz=UTC)
            if isinstance(created, int | float)
            else None
        )
        if finished_at is not None and isinstance(duration, int | float):
            started_at = finished_at - timedelta(seconds=float(duration))
        else:
            started_at = finished_at or datetime.now(UTC)

        return TestRun(
            suite_name=meta.suite_name,
            started_at=started_at,
            finished_at=finished_at,
            results=results,
            source_format=self.format_name,
            commit_sha=meta.commit_sha,
            branch=meta.branch,
            ci_run_url=meta.ci_run_url,
            environment=meta.environment,
        )


register(PytestJsonParser())
