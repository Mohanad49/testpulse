"""Playwright JSON reporter parser.

Structurally the richest format supported here, and the only one that records
every attempt of a retried test rather than just the final verdict. That makes
it the primary source of same-commit flake evidence in Phase 2.

Shape: ``suites`` nest arbitrarily deep (one level per file and per
``test.describe`` block); each leaf suite holds ``specs``; each spec holds
``tests`` (one per project, e.g. chromium and firefox); each test holds
``results``, one per attempt.
"""

from __future__ import annotations

import json
import re
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

# Playwright colourises error messages for the terminal and leaves the escape
# codes in the JSON report. Storing them would push control characters into the
# database and into every dashboard that later renders a failure message.
_ANSI = re.compile(r"\x1b\[[0-9;]*m")

# Per-attempt statuses. `timedOut` is mapped to failed rather than error because
# a timeout is overwhelmingly a symptom of the thing under test being slow or
# stuck, which is a product failure, whereas `interrupted` means the run itself
# was killed and the test never reached a verdict. This is a judgement call and
# the original wording is preserved in raw_status either way.
_STATUS_MAP = {
    "passed": TestStatus.PASSED,
    "failed": TestStatus.FAILED,
    "timedOut": TestStatus.FAILED,
    "skipped": TestStatus.SKIPPED,
    "interrupted": TestStatus.ERROR,
}


def _strip_ansi(value: str | None) -> str | None:
    return _ANSI.sub("", value) if value else None


def _iter_specs(suite: dict[str, Any], trail: list[str]) -> list[tuple[dict[str, Any], list[str]]]:
    """Flatten the nested suite tree into ``(spec, describe_titles)`` pairs.

    The first suite level is the spec file and the rest are ``describe`` blocks.
    The file title is dropped from the trail because it is already available as
    the spec's ``file`` field; keeping both would duplicate it inside the class
    name segment of every ``test_id``.
    """
    found: list[tuple[dict[str, Any], list[str]]] = []
    for spec in suite.get("specs", []):
        found.append((spec, trail))
    for child in suite.get("suites", []):
        found.extend(_iter_specs(child, [*trail, child.get("title", "")]))
    return found


def _parse_iso(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


class PlaywrightJsonParser:
    """Parses Playwright's ``json`` reporter output into a normalised run."""

    format_name = "playwright"

    def parse(self, path: Path, meta: RunMetadata) -> TestRun:
        if not path.is_file():
            raise ParseError(f"Playwright JSON report not found at {path}")
        try:
            document = json.loads(path.read_text())
        except json.JSONDecodeError as exc:
            raise ParseError(f"{path} is not valid JSON: {exc}") from exc
        if not isinstance(document, dict) or "suites" not in document:
            raise ParseError(
                f"{path} does not look like a Playwright JSON report "
                "(no top-level 'suites' key)"
            )

        results: list[TestResult] = []
        for root_suite in document["suites"]:
            # The root suite's own title is the file name, so the describe trail
            # starts empty and is built from its children.
            for spec, describes in _iter_specs(root_suite, []):
                results.extend(self._results_for_spec(spec, describes))

        stats = document.get("stats", {})
        started_at = _parse_iso(stats.get("startTime")) or datetime.now(UTC)
        duration_ms = stats.get("duration")
        finished_at = (
            started_at + timedelta(milliseconds=float(duration_ms))
            if isinstance(duration_ms, int | float)
            else None
        )

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

    def _results_for_spec(
        self, spec: dict[str, Any], describes: list[str]
    ) -> list[TestResult]:
        """Turn one spec into one result per project it ran under."""
        title = spec.get("title", "")
        file_path = spec.get("file")
        class_name = " > ".join(d for d in describes if d) or None
        produced: list[TestResult] = []

        for test in spec.get("tests", []):
            attempts = test.get("results", [])
            if not attempts:
                continue
            # The last attempt is the run's verdict — it is what CI reported and
            # what a developer saw. Earlier attempts are not discarded: the fact
            # that they happened survives as retry_count, and because Playwright
            # only retries a test that did not pass, retry_count > 0 combined
            # with a passing final status is by itself proof of same-run
            # disagreement. Phase 2's high-precision flake strategy reads exactly
            # that, so no extra field is needed to preserve it.
            final = max(attempts, key=lambda a: a.get("retry", 0))
            raw_status = str(final.get("status", "unknown"))
            error = final.get("error") or {}

            project = test.get("projectName") or None
            # A test that runs under several projects is a distinct test per
            # project: chromium and firefox fail independently, so their
            # histories must not be joined.
            name_for_id = f"{title} [{project}]" if project else title

            produced.append(
                TestResult(
                    test_id=build_test_id(file_path, class_name, name_for_id),
                    display_name=title,
                    status=_STATUS_MAP.get(raw_status, TestStatus.ERROR),
                    duration_ms=int(final.get("duration", 0)),
                    raw_status=raw_status,
                    file_path=file_path,
                    class_name=class_name,
                    test_name=title,
                    failure_message=_strip_ansi(error.get("message")),
                    failure_stack=_strip_ansi(error.get("stack")),
                    retry_count=int(final.get("retry", 0)),
                    attachments=[
                        str(a["path"])
                        for attempt in attempts
                        for a in attempt.get("attachments", [])
                        if a.get("path")
                    ],
                )
            )
        return produced


register(PlaywrightJsonParser())
