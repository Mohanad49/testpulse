"""pytest-json-report parser tests.

The fixture and `junit/pytest-suite.xml` were emitted by the same pytest
session, which makes cross-format agreement a usable assertion: two parsers
reading two reports of one run must agree on what happened.
"""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from datetime import UTC, datetime

import pytest

from conftest import FIXTURES
from testpulse_core.models import RunMetadata, TestStatus
from testpulse_core.parsers.base import ParseError
from testpulse_core.parsers.junit import JUnitParser
from testpulse_core.parsers.pytest_json import PytestJsonParser

META = RunMetadata(suite_name="sample-suite", commit_sha="c" * 40)
REPORT = FIXTURES / "pytest" / "pytest-report.json"


@pytest.fixture
def run():
    return PytestJsonParser().parse(REPORT, META)


@pytest.fixture
def by_name(run):
    return {r.display_name: r for r in run.results}


def test_counts_match_the_reports_own_summary(run):
    summary = json.loads(REPORT.read_text())["summary"]
    assert run.total == summary["total"]
    assert run.passed == summary["passed"]
    assert run.failed == summary["failed"]
    assert run.skipped == summary["skipped"]
    assert run.errored == summary["error"]


def test_agrees_with_the_junit_report_of_the_same_session(run):
    junit_run = JUnitParser().parse(FIXTURES / "junit" / "pytest-suite.xml", META)
    assert run.total == junit_run.total
    assert (run.passed, run.failed, run.skipped, run.errored) == (
        junit_run.passed,
        junit_run.failed,
        junit_run.skipped,
        junit_run.errored,
    )


def test_started_at_is_derived_from_the_end_time(run):
    # `created` is when the report was written, so it is the end of the session.
    # The JUnit report of the same session records the true start; the two must
    # land on the same instant.
    junit_ts = ET.parse(FIXTURES / "junit" / "pytest-suite.xml").getroot()
    suite_timestamp = junit_ts.find("testsuite").get("timestamp")
    expected_start = datetime.fromisoformat(suite_timestamp).astimezone(UTC)
    drift = abs((run.started_at - expected_start).total_seconds())
    assert drift < 0.01, f"start time is off by {drift}s - `created` was likely read as a start"


def test_finished_at_is_after_started_at(run):
    assert run.finished_at is not None
    assert run.finished_at > run.started_at


def test_nodeid_splits_into_file_class_and_name(by_name):
    result = by_name["test_rejects_expired_coupon"]
    assert result.file_path == "test_sample_suite.py"
    assert result.class_name == "TestCheckout"
    assert result.test_name == "test_rejects_expired_coupon"


def test_module_level_test_has_no_class(by_name):
    assert by_name["test_login_succeeds"].class_name is None


def test_parametrised_names_keep_their_suffix(run):
    names = {r.display_name for r in run.results if "test_timezone_offset" in r.display_name}
    assert names == {"test_timezone_offset[Africa/Cairo]", "test_timezone_offset[Asia/Kathmandu]"}


def test_setup_error_is_reported_as_error_with_its_message(by_name):
    # This test never ran: its fixture raised. The traceback lives under the
    # `setup` phase, so a parser that only reads `call` would lose it entirely.
    result = by_name["test_reads_from_backend"]
    assert result.status is TestStatus.ERROR
    assert result.failure_message is not None
    assert "could not connect to fixture backend on port 5432" in result.failure_message


def test_call_failure_carries_message_and_longrepr(by_name):
    result = by_name["test_cart_total_is_correct"]
    assert result.status is TestStatus.FAILED
    assert "expected total 30" in (result.failure_message or "")
    assert "assert (10 * 3) == 31" in (result.failure_stack or "")


def test_duration_includes_fixture_time_not_just_the_assertion(run):
    document = json.loads(REPORT.read_text())
    by_nodeid = {t["nodeid"]: t for t in document["tests"]}
    target = by_nodeid["test_sample_suite.py::test_cart_total_is_correct"]
    phase_total = sum(target[p]["duration"] for p in ("setup", "call", "teardown"))
    parsed = next(r for r in run.results if r.display_name == "test_cart_total_is_correct")
    assert parsed.duration_ms == round(phase_total * 1000)
    # And it is strictly more than the call phase alone.
    assert parsed.duration_ms >= round(target["call"]["duration"] * 1000)


def test_retry_count_is_unknown(run):
    assert all(r.retry_count is None for r in run.results)


def test_non_pytest_json_is_rejected(tmp_path):
    bogus = tmp_path / "other.json"
    bogus.write_text(json.dumps({"suites": []}))
    with pytest.raises(ParseError, match="does not look like a pytest-json-report"):
        PytestJsonParser().parse(bogus, META)


def test_entry_without_nodeid_is_rejected(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"tests": [{"outcome": "passed"}]}))
    with pytest.raises(ParseError, match="no nodeid"):
        PytestJsonParser().parse(bad, META)
