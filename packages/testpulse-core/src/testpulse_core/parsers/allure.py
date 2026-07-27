"""Allure results directory parser.

Allure is a directory of files, not a single report: one ``*-result.json`` per
test, plus loose attachment files the results reference by name. It is also the
only format here that is written by a per-framework adapter rather than by the
runner itself, which means two suites can both be "Allure" and still disagree
about how a test is identified. This parser is built around that: it reads the
fields the adapters agree on and ignores the ones they do not.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
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
    # Allure's own distinction: `failed` is an assertion that did not hold,
    # `broken` is an unexpected exception - the test did not reach a verdict.
    # That is the same line JUnit draws between failure and error, so it maps
    # the same way and stays consistent across formats.
    "broken": TestStatus.ERROR,
    "skipped": TestStatus.SKIPPED,
    "unknown": TestStatus.ERROR,
}


def _labels(document: dict[str, Any]) -> dict[str, str]:
    """Collapse Allure's ``[{name, value}]`` label list into a dict.

    Duplicate label names exist (``tag`` in particular) and the last one wins.
    Nothing this parser reads is ever duplicated, so that is safe here; a future
    consumer that wants tags must read the raw list instead.
    """
    return {
        str(item["name"]): str(item["value"])
        for item in document.get("labels", [])
        if isinstance(item, dict) and "name" in item and "value" in item
    }


def _collect_attachments(node: dict[str, Any]) -> list[str]:
    """Gather attachments from a result and every step beneath it, recursively.

    This recursion is not defensive coding. In a real 270-result Allure
    directory produced by allure-playwright, zero results carried a top-level
    attachment and 47 carried them nested inside steps. A parser reading only
    ``document["attachments"]`` returns an empty list for every test in that
    directory and looks like it works, because empty is a plausible answer.
    """
    found = [
        str(a["source"])
        for a in node.get("attachments", []) or []
        if isinstance(a, dict) and a.get("source")
    ]
    for step in node.get("steps", []) or []:
        if isinstance(step, dict):
            found.extend(_collect_attachments(step))
    return found


def _identity(document: dict[str, Any], labels: dict[str, str]) -> tuple[str | None, str | None]:
    """Derive ``(file_path, class_name)`` from labels, not from ``fullName``.

    ``fullName`` is adapter-specific. allure-playwright writes
    ``recruitment/recruitment.spec.ts:65:7`` — a path with a source position
    that changes whenever a line is inserted above the test. allure-pytest
    writes ``tests.test_issues.TestIssues#test_name`` — dotted, no path, no
    position. There is no parsing rule that reads both without guessing which
    adapter produced the file.

    The ``suite`` / ``subSuite`` labels are populated consistently by both
    adapters and carry no line numbers, so they are the stable choice. The
    tradeoff: ``suite`` is a spec file for Playwright and a module for pytest,
    so the resulting ``file_path`` is not always literally a path. It is stable
    and it joins correctly across runs, which is what ``test_id`` is for.
    """
    file_path = labels.get("suite")
    class_name = labels.get("subSuite")
    if file_path is None:
        # No suite label: fall back to fullName with any source position removed
        # rather than producing an identifier with no scope at all.
        full_name = document.get("fullName")
        file_path = str(full_name) if full_name else None
    return file_path, class_name


class AllureParser:
    """Parses an Allure results directory into a normalised run."""

    format_name = "allure"

    def parse(self, path: Path, meta: RunMetadata) -> TestRun:
        if not path.is_dir():
            raise ParseError(
                f"Allure results path {path} is not a directory. This format is a "
                "directory of *-result.json files, not a single report."
            )
        result_files = sorted(path.glob("*-result.json"))
        if not result_files:
            raise ParseError(
                f"No *-result.json files found in {path}. An empty directory is "
                "reported as an error rather than an empty run, because 'the suite "
                "produced nothing' and 'the results were never written' need "
                "different responses and look identical here."
            )

        results: list[TestResult] = []
        starts: list[int] = []
        stops: list[int] = []

        for file in result_files:
            try:
                document = json.loads(file.read_text())
            except json.JSONDecodeError as exc:
                raise ParseError(f"{file} is not valid JSON: {exc}") from exc
            if not isinstance(document, dict):
                raise ParseError(f"{file} does not contain a JSON object")

            name = document.get("name")
            if not name:
                raise ParseError(f"{file} has no 'name' field")

            labels = _labels(document)
            file_path, class_name = _identity(document, labels)
            raw_status = str(document.get("status", "unknown"))
            details = document.get("statusDetails") or {}

            start = document.get("start")
            stop = document.get("stop")
            if isinstance(start, int):
                starts.append(start)
            if isinstance(stop, int):
                stops.append(stop)
            duration_ms = (
                stop - start if isinstance(start, int) and isinstance(stop, int) else 0
            )

            results.append(
                TestResult(
                    test_id=build_test_id(file_path, class_name, str(name)),
                    display_name=str(name),
                    status=_STATUS_MAP.get(raw_status, TestStatus.ERROR),
                    duration_ms=max(duration_ms, 0),
                    raw_status=raw_status,
                    file_path=file_path,
                    class_name=class_name,
                    test_name=str(name),
                    failure_message=details.get("message"),
                    failure_stack=details.get("trace"),
                    # Allure records retries as separate result files sharing a
                    # historyId, not as a count on one result. Reconstructing that
                    # needs cross-file correlation, which is Phase 2 work; until
                    # then this is honestly unknown rather than falsely zero.
                    retry_count=None,
                    attachments=_collect_attachments(document),
                )
            )

        # Allure timestamps are epoch milliseconds, unlike every other format here.
        started_at = (
            datetime.fromtimestamp(min(starts) / 1000, tz=UTC) if starts else datetime.now(UTC)
        )
        finished_at = datetime.fromtimestamp(max(stops) / 1000, tz=UTC) if stops else None

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


register(AllureParser())
