"""JUnit parser tests, driven by real reporter output.

Two producers are exercised deliberately: pytest (which emits every status and
uses dotted classnames) and Newman (which emits none of them and omits `file`
entirely). A parser that only ever saw one producer's output would encode that
producer's habits as if they were the format.
"""

from __future__ import annotations

from datetime import UTC

import pytest

from conftest import FIXTURES
from testpulse_core.models import RunMetadata, TestStatus
from testpulse_core.parsers.base import ParseError
from testpulse_core.parsers.junit import JUnitParser

META = RunMetadata(
    suite_name="sample-suite",
    commit_sha="a" * 40,
    branch="main",
    environment="local",
)


@pytest.fixture
def pytest_run():
    return JUnitParser().parse(FIXTURES / "junit" / "pytest-suite.xml", META)


@pytest.fixture
def newman_run():
    return JUnitParser().parse(FIXTURES / "junit" / "newman-restful-booker.xml", META)


def test_counts_match_the_reports_own_summary(pytest_run):
    # The fixture's <testsuite> element claims 9 tests, 2 failures, 1 error,
    # 1 skipped. Asserting against the producer's own tally catches a parser
    # that silently drops cases.
    assert pytest_run.total == 9
    assert pytest_run.passed == 5
    assert pytest_run.failed == 2
    assert pytest_run.errored == 1
    assert pytest_run.skipped == 1


def test_failure_and_error_are_not_collapsed(pytest_run):
    by_name = {r.display_name: r for r in pytest_run.results}
    # An assertion that did not hold.
    assert by_name["test_cart_total_is_correct"].status is TestStatus.FAILED
    # A fixture that raised, so the test never ran at all.
    assert by_name["test_reads_from_backend"].status is TestStatus.ERROR
    assert by_name["test_reads_from_backend"].raw_status == "error"


def test_failure_message_and_stack_are_both_captured(pytest_run):
    result = next(r for r in pytest_run.results if r.display_name == "test_cart_total_is_correct")
    assert result.failure_message is not None
    assert "expected total 30 for 3 items at 10 each" in result.failure_message
    assert result.failure_stack is not None
    assert "test_sample_suite.py:13" in result.failure_stack


def test_skip_reason_is_retained(pytest_run):
    result = next(r for r in pytest_run.results if r.display_name == "test_export_to_csv")
    assert result.status is TestStatus.SKIPPED
    assert result.failure_message == "feature behind a flag not enabled in CI"


def test_seconds_are_converted_to_integer_milliseconds(pytest_run):
    # The fixture reports time="0.001" seconds. A parser that forgot the
    # conversion would produce 0; one that double-converted would produce 1000.
    durations = {r.display_name: r.duration_ms for r in pytest_run.results}
    assert durations["test_login_succeeds"] == 1
    assert all(isinstance(d, int) for d in durations.values())


def test_parametrised_cases_stay_distinct(pytest_run):
    ids = {r.test_id for r in pytest_run.results if "test_timezone_offset" in r.test_id}
    assert len(ids) == 2, "parametrised variants must not collapse into one test_id"


def test_class_scoped_tests_carry_their_class(pytest_run):
    result = next(
        r for r in pytest_run.results if r.display_name == "test_rejects_expired_coupon"
    )
    assert result.class_name == "test_sample_suite.TestCheckout"
    assert result.test_id.endswith("::test_sample_suite.TestCheckout::test_rejects_expired_coupon")


def test_retry_count_is_unknown_not_zero(pytest_run):
    # JUnit has no portable retry representation. Reporting 0 here would let
    # Phase 2 conclude "this test was not retried" from a format that cannot say.
    assert all(r.retry_count is None for r in pytest_run.results)


def test_newman_report_without_file_attributes_still_yields_ids(newman_run):
    assert newman_run.total > 0
    assert all(r.file_path is None for r in newman_run.results)
    # test_id degrades to "::classname::name" rather than failing.
    assert all(r.test_id.startswith("::") for r in newman_run.results)
    assert all(r.class_name for r in newman_run.results)


def test_all_passing_report_reports_no_failures(newman_run):
    assert newman_run.failed == 0
    assert newman_run.errored == 0
    assert newman_run.passed == newman_run.total


def test_timestamps_are_normalised_to_utc(newman_run, pytest_run):
    # Newman writes a trailing Z; pytest writes a numeric offset (+03:00).
    # Both must land on an aware UTC datetime.
    for run in (newman_run, pytest_run):
        assert run.started_at.tzinfo is not None
        assert run.started_at.utcoffset() == UTC.utcoffset(None)


def test_ci_metadata_is_carried_through(pytest_run):
    assert pytest_run.commit_sha == "a" * 40
    assert pytest_run.branch == "main"
    assert pytest_run.environment == "local"
    assert pytest_run.source_format == "junit"


def test_missing_file_raises_parse_error(tmp_path):
    with pytest.raises(ParseError, match="not found"):
        JUnitParser().parse(tmp_path / "nope.xml", META)
