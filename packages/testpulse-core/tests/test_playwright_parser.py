"""Playwright JSON parser tests.

The fixture was produced by a suite written to contain the cases that matter:
a test that fails then passes on retry, one that fails all three attempts, one
that times out, and one that is skipped.
"""

from __future__ import annotations

import json

import pytest

from conftest import FIXTURES
from testpulse_core.models import RunMetadata, TestStatus
from testpulse_core.parsers.base import ParseError
from testpulse_core.parsers.playwright_json import PlaywrightJsonParser

META = RunMetadata(suite_name="booking-e2e", commit_sha="b" * 40, environment="chromium-ci")
REPORT = FIXTURES / "playwright" / "playwright-report.json"


@pytest.fixture
def run():
    return PlaywrightJsonParser().parse(REPORT, META)


@pytest.fixture
def by_name(run):
    return {r.display_name: r for r in run.results}


def test_nested_describe_blocks_are_flattened_into_results(run):
    # Six specs live two suite levels deep (file -> describe).
    assert run.total == 6


def test_status_counts_match_the_reports_own_stats(run):
    # stats says expected=2, unexpected=2, flaky=1, skipped=1. The flaky test's
    # final attempt passed, so it counts as passed here: 3 passed, 2 failed,
    # 1 skipped.
    assert run.passed == 3
    assert run.failed == 2
    assert run.skipped == 1


def test_final_attempt_is_the_verdict_not_the_first(by_name):
    # This test failed on attempt 0 and passed on attempt 1. Reporting the first
    # attempt would mark a passing suite as failed.
    flaky = by_name["reschedules across a DST boundary"]
    assert flaky.status is TestStatus.PASSED
    assert flaky.retry_count == 1


def test_retry_count_plus_pass_is_the_same_run_flake_signal(by_name):
    # Playwright only retries a test that did not pass. A passing final status
    # with retry_count > 0 therefore means the same code produced two different
    # outcomes in one run - the strongest flake evidence there is, and what
    # Phase 2's high-precision strategy consumes.
    flaky = by_name["reschedules across a DST boundary"]
    assert flaky.status is TestStatus.PASSED and flaky.retry_count > 0


def test_exhausted_retries_stay_failed(by_name):
    hard_fail = by_name["double booking is rejected"]
    assert hard_fail.status is TestStatus.FAILED
    assert hard_fail.retry_count == 2


def test_timed_out_maps_to_failed_but_keeps_its_original_wording(by_name):
    timed_out = by_name["availability lookup completes in time"]
    assert timed_out.status is TestStatus.FAILED
    assert timed_out.raw_status == "timedOut"


def test_retry_count_is_zero_not_none_because_the_format_reports_it(by_name):
    # Contrast with JUnit, where retry_count is None. Playwright can say "no
    # retries happened", and that assertion is itself information.
    assert by_name["creates a booking in the host timezone"].retry_count == 0


def test_ansi_escape_codes_are_stripped_from_failure_messages(run):
    raw = REPORT.read_text()
    assert "\\u001b[" in raw, "fixture should contain ANSI codes, otherwise this proves nothing"
    for result in run.results:
        assert "\x1b[" not in (result.failure_message or "")
        assert "\x1b[" not in (result.failure_stack or "")


def test_failure_message_survives_stripping(by_name):
    message = by_name["double booking is rejected"].failure_message
    assert message is not None
    assert "slot was released" in message


def test_describe_block_becomes_the_class_name(by_name):
    assert by_name["creates a booking in the host timezone"].class_name == "Booking flow"


def test_attachments_are_collected_across_every_attempt(by_name):
    # The three-attempt failure produced an error-context attachment per attempt.
    assert len(by_name["double booking is rejected"].attachments) == 3


def test_durations_are_already_milliseconds(by_name):
    # 3000ms configured timeout; the report gives ~3003. A parser that applied
    # the JUnit seconds conversion would yield ~3_003_000.
    assert 2500 < by_name["availability lookup completes in time"].duration_ms < 5000


def test_run_window_comes_from_stats(run):
    assert run.finished_at is not None
    assert run.finished_at > run.started_at


def test_non_playwright_json_is_rejected(tmp_path):
    bogus = tmp_path / "other.json"
    bogus.write_text(json.dumps({"tests": []}))
    with pytest.raises(ParseError, match="does not look like a Playwright JSON report"):
        PlaywrightJsonParser().parse(bogus, META)


def test_invalid_json_is_rejected(tmp_path):
    broken = tmp_path / "broken.json"
    broken.write_text('{"suites": [')
    with pytest.raises(ParseError, match="not valid JSON"):
        PlaywrightJsonParser().parse(broken, META)
