"""JUnit XML parser.

JUnit XML is the universal fallback: pytest, Playwright, Maestro, Newman, Maven
and almost everything else can emit it. It is also the least informative format
supported here, because the schema was never standardised — there is no
specification, only a family of dialects that agree on the element names and
disagree on nearly every attribute. This parser reads the intersection that all
producers honour and treats anything beyond it as optional.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import UTC, datetime, timedelta
from pathlib import Path
from xml.etree.ElementTree import Element

from testpulse_core.models import (
    RunMetadata,
    TestResult,
    TestRun,
    TestStatus,
    build_test_id,
)
from testpulse_core.parsers.base import ParseError, register


def _status_from_case(case: Element) -> tuple[TestStatus, str]:
    """Derive a status from which child element the testcase carries.

    JUnit encodes status structurally rather than as an attribute: a passing
    test is simply one with no ``failure``, ``error`` or ``skipped`` child.
    Absence-as-success is why a truncated file is dangerous — every test whose
    closing tag was lost would read as passing, which is why
    :func:`parse` refuses to salvage malformed XML.

    ``failure`` and ``error`` are kept distinct. Producers use ``failure`` for an
    assertion that did not hold and ``error`` for the test not completing at all
    (a fixture blew up, the process died). Those need different responses from a
    team, so collapsing them here would destroy information the report bothered
    to record.
    """
    if case.find("error") is not None:
        return TestStatus.ERROR, "error"
    if case.find("failure") is not None:
        return TestStatus.FAILED, "failure"
    if case.find("skipped") is not None:
        return TestStatus.SKIPPED, "skipped"
    return TestStatus.PASSED, "passed"


def _failure_detail(case: Element) -> tuple[str | None, str | None]:
    """Return ``(message, stack)`` for a non-passing testcase.

    The ``message`` attribute holds a one-line summary; the element's text holds
    the full traceback. Producers are inconsistent about which they populate, so
    both are read and either may be ``None``.
    """
    for tag in ("error", "failure", "skipped"):
        node = case.find(tag)
        if node is not None:
            message = node.get("message")
            stack = (node.text or "").strip() or None
            return message, stack
    return None, None


def _duration_ms(raw: str | None) -> int:
    """Convert a JUnit ``time`` attribute to integer milliseconds.

    JUnit reports seconds as a float; every other format here reports
    milliseconds. Getting this wrong is a silent 1000x error that looks like a
    performance regression rather than a bug, so the conversion is deliberately
    isolated and directly tested.
    """
    if raw is None:
        return 0
    try:
        return round(float(raw) * 1000)
    except ValueError:
        return 0


def _parse_timestamp(raw: str | None) -> datetime | None:
    """Parse a suite ``timestamp`` attribute into an aware UTC datetime.

    Producers emit either a trailing ``Z`` (Newman) or a numeric offset
    (pytest). A timestamp with no zone at all is treated as UTC rather than as
    local time: assuming the ingesting machine's zone would make the same report
    land at different instants depending on who ran the ingest.
    """
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _iter_suites(root: Element) -> list[Element]:
    """Return every ``testsuite`` element, whatever the root element is.

    Some producers wrap suites in ``<testsuites>``; others emit a single bare
    ``<testsuite>`` as the document root. Nested suites are flattened, since the
    hierarchy carries no information this schema keeps.
    """
    if root.tag == "testsuite":
        return [root, *root.findall(".//testsuite")]
    return root.findall(".//testsuite")


class JUnitParser:
    """Parses JUnit XML into a normalised run."""

    format_name = "junit"

    def parse(self, path: Path, meta: RunMetadata) -> TestRun:
        if not path.is_file():
            raise ParseError(f"JUnit report not found at {path}")
        try:
            tree = ET.parse(path)
        except ET.ParseError as exc:
            raise ParseError(f"{path} is not well-formed XML: {exc}") from exc

        root = tree.getroot()
        if root.tag not in {"testsuites", "testsuite"}:
            raise ParseError(
                f"{path} has root element <{root.tag}>; expected <testsuites> or <testsuite>"
            )

        suites = _iter_suites(root)
        results: list[TestResult] = []
        timestamps: list[datetime] = []
        total_ms = 0

        for suite in suites:
            suite_ts = _parse_timestamp(suite.get("timestamp"))
            if suite_ts is not None:
                timestamps.append(suite_ts)
            for case in suite.findall("testcase"):
                name = case.get("name")
                if name is None:
                    raise ParseError(
                        f"{path} contains a <testcase> with no name attribute "
                        f"in suite {suite.get('name')!r}"
                    )
                status, raw_status = _status_from_case(case)
                message, stack = _failure_detail(case)
                duration = _duration_ms(case.get("time"))
                total_ms += duration
                # `file` is optional and most producers omit it; pytest and Newman
                # both put the module or collection path in `classname` instead.
                file_path = case.get("file")
                class_name = case.get("classname")
                results.append(
                    TestResult(
                        test_id=build_test_id(file_path, class_name, name),
                        display_name=name,
                        status=status,
                        duration_ms=duration,
                        raw_status=raw_status,
                        file_path=file_path,
                        class_name=class_name,
                        test_name=name,
                        failure_message=message,
                        failure_stack=stack,
                        # JUnit has no portable representation of retries. Surefire's
                        # <rerunFailure> exists but almost nothing else emits it, so
                        # this stays None: "unknown", not "zero".
                        retry_count=None,
                        attachments=[],
                    )
                )

        # JUnit records when suites started but never when the run ended, so the
        # end is reconstructed as start + summed test time. That undercounts on a
        # parallel run (wall-clock is shorter than the sum) and overcounts nothing.
        # It is an estimate, and Phase 2's duration trends should read suite
        # duration from summed test time rather than from this field.
        started_at = min(timestamps) if timestamps else datetime.now(UTC)
        return TestRun(
            suite_name=meta.suite_name,
            started_at=started_at,
            finished_at=started_at + timedelta(milliseconds=total_ms),
            results=results,
            source_format=self.format_name,
            commit_sha=meta.commit_sha,
            branch=meta.branch,
            ci_run_url=meta.ci_run_url,
            environment=meta.environment,
        )


register(JUnitParser())
