"""Allure parser tests.

Two producers, both real: allure-playwright output captured from the
orangehrm-playwright suite, and allure-pytest output from the github-api-tests
suite. They disagree about `fullName` grammar, which is the whole reason this
parser identifies tests from labels instead.
"""

from __future__ import annotations

import json

import pytest

from conftest import FIXTURES
from testpulse_core.models import RunMetadata, TestStatus
from testpulse_core.parsers.allure import AllureParser
from testpulse_core.parsers.base import ParseError

META = RunMetadata(suite_name="orangehrm-e2e", commit_sha="d" * 40)
PLAYWRIGHT_DIR = FIXTURES / "allure" / "playwright-producer"
PYTEST_DIR = FIXTURES / "allure" / "pytest-producer"


@pytest.fixture
def pw_run():
    return AllureParser().parse(PLAYWRIGHT_DIR, META)


@pytest.fixture
def py_run():
    return AllureParser().parse(PYTEST_DIR, META)


def test_reads_every_result_file_in_the_directory(pw_run):
    assert pw_run.total == len(list(PLAYWRIGHT_DIR.glob("*-result.json")))


def test_broken_maps_to_error_not_failed(pw_run):
    # Allure separates `failed` (an assertion did not hold) from `broken` (an
    # unexpected exception). That is the same line JUnit draws between failure
    # and error, so it maps the same way.
    broken = next(r for r in pw_run.results if r.raw_status == "broken")
    assert broken.status is TestStatus.ERROR

    failed = next(r for r in pw_run.results if r.raw_status == "failed")
    assert failed.status is TestStatus.FAILED


def test_all_four_statuses_are_represented_in_the_fixture(pw_run):
    assert {r.raw_status for r in pw_run.results} == {"passed", "failed", "broken", "skipped"}


def test_attachments_nested_inside_steps_are_found(pw_run):
    # The regression this parser exists to avoid. In the source directory these
    # fixtures came from, zero of 270 results had a top-level attachment and 47
    # had them nested in steps. Reading document["attachments"] returns [] for
    # every test and looks correct.
    # Not one fixture carries a top-level attachment...
    for f in PLAYWRIGHT_DIR.glob("*-result.json"):
        assert json.loads(f.read_text()).get("attachments") == []

    # ...yet three of the four results have them, and the failed one carries the
    # full set: screenshot, video, error-context and trace.
    with_attachments = [r for r in pw_run.results if r.attachments]
    assert len(with_attachments) == 3

    failed = next(r for r in pw_run.results if r.raw_status == "failed")
    assert len(failed.attachments) == 4
    assert any(a.endswith(".png") for a in failed.attachments)
    assert any(a.endswith(".webm") for a in failed.attachments)
    assert any(a.endswith(".zip") for a in failed.attachments)


def test_identity_comes_from_labels_not_from_fullname(pw_run):
    # allure-playwright writes fullName as "recruitment/recruitment.spec.ts:65:7".
    # Using it would put a line number in the test_id.
    for result in pw_run.results:
        assert ":" not in (result.file_path or ""), (
            "file_path must not carry a source position"
        )
    assert {r.file_path for r in pw_run.results} == {
        "auth/login.spec.ts",
        "recruitment/recruitment.spec.ts",
    }
    assert {r.class_name for r in pw_run.results} == {
        "Authentication Tests",
        "Recruitment Tests",
    }


def test_the_two_producers_disagree_about_fullname(pw_run, py_run):
    # Documents the reason for the label-based approach rather than assuming it.
    pw_full = {
        json.loads(f.read_text())["fullName"] for f in PLAYWRIGHT_DIR.glob("*-result.json")
    }
    py_full = {json.loads(f.read_text())["fullName"] for f in PYTEST_DIR.glob("*-result.json")}
    assert any(":" in v for v in pw_full), "playwright fullName carries a source position"
    assert any("#" in v for v in py_full), "pytest fullName uses a dotted path with #"
    # Despite that, both yield usable identities.
    assert all(r.test_id for r in [*pw_run.results, *py_run.results])


def test_pytest_producer_identity(py_run):
    # allure-pytest fills the same suite/subSuite labels with a module and a
    # class, so the identity shape is consistent even though the vocabulary
    # differs from Playwright's file-and-describe.
    result = next(r for r in py_run.results if r.display_name.startswith("test_"))
    assert result.file_path == "test_repositories"
    assert result.class_name == "TestRepositories"
    assert result.test_id == "test_repositories::TestRepositories::" + result.display_name


def test_duration_is_computed_from_epoch_millisecond_bounds(pw_run):
    for f in PLAYWRIGHT_DIR.glob("*-result.json"):
        raw = json.loads(f.read_text())
        parsed = next(r for r in pw_run.results if r.display_name == raw["name"])
        assert parsed.duration_ms == raw["stop"] - raw["start"]
        assert parsed.duration_ms >= 0


def test_failure_message_and_trace_are_captured(pw_run):
    failed = next(r for r in pw_run.results if r.raw_status == "failed")
    assert failed.failure_message
    assert failed.failure_stack


def test_skip_reason_is_captured(pw_run):
    skipped = next(r for r in pw_run.results if r.status is TestStatus.SKIPPED)
    assert "No Job Titles available" in (skipped.failure_message or "")


def test_retry_count_is_unknown_pending_cross_file_correlation(pw_run):
    assert all(r.retry_count is None for r in pw_run.results)


def test_run_window_is_derived_from_the_earliest_and_latest_result(pw_run):
    assert pw_run.finished_at is not None
    assert pw_run.finished_at >= pw_run.started_at
    assert pw_run.started_at.tzinfo is not None


def test_a_file_path_is_rejected_with_a_useful_message():
    with pytest.raises(ParseError, match="is not a directory"):
        AllureParser().parse(PLAYWRIGHT_DIR / "nonexistent-result.json", META)


def test_empty_directory_is_an_error_not_an_empty_run(tmp_path):
    # "the suite produced nothing" and "the results were never written" look
    # identical here and need different responses, so this refuses to guess.
    with pytest.raises(ParseError, match=r"No \*-result\.json files"):
        AllureParser().parse(tmp_path, META)


def test_malformed_result_file_is_rejected(tmp_path):
    (tmp_path / "bad-result.json").write_text("{not json")
    with pytest.raises(ParseError, match="not valid JSON"):
        AllureParser().parse(tmp_path, META)


def test_result_without_a_name_is_rejected(tmp_path):
    (tmp_path / "x-result.json").write_text(json.dumps({"status": "passed"}))
    with pytest.raises(ParseError, match="no 'name' field"):
        AllureParser().parse(tmp_path, META)
