"""CLI tests.

The CLI is the surface CI actually calls, so the assertions here are about exit
codes and stderr: those are the only two things a pipeline step reads reliably.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from typer.testing import CliRunner

from conftest import FIXTURES
from testpulse_core.cli import (
    EXIT_DUPLICATE,
    EXIT_FLAKY_FOUND,
    EXIT_PARSE_ERROR,
    EXIT_USAGE,
    app,
)
from testpulse_core.storage.db import create_db_engine, session_scope
from testpulse_core.storage.schema import Base, TestResultRow, TestRunRow

runner = CliRunner()


@pytest.fixture
def db_url(tmp_path):
    url = f"sqlite:///{tmp_path / 'cli.db'}"
    engine = create_db_engine(url)
    Base.metadata.create_all(engine)
    engine.dispose()
    return url


def ingest_args(db_url, **overrides):
    args = {
        "--path": str(FIXTURES / "junit" / "pytest-suite.xml"),
        "--suite": "sample-suite",
        "--format": "junit",
        "--commit": "a" * 40,
        "--branch": "main",
        "--env": "local",
        "--database-url": db_url,
    }
    args.update(overrides)
    flat = []
    for key, value in args.items():
        if value is not None:
            flat.extend([key, value])
    return ["ingest", *flat]


def test_ingest_succeeds_and_reports_what_it_wrote(db_url):
    result = runner.invoke(app, ingest_args(db_url))
    assert result.exit_code == 0, result.output
    assert "9 results" in result.output
    assert "5 passed" in result.output
    assert "2 failed" in result.output


def test_ingest_actually_persists(db_url):
    runner.invoke(app, ingest_args(db_url))
    engine = create_db_engine(db_url)
    with session_scope(engine) as session:
        run = session.execute(select(TestRunRow)).scalar_one()
        assert run.total == 9
        assert run.commit_sha == "a" * 40
        assert len(session.execute(select(TestResultRow)).scalars().all()) == 9
    engine.dispose()


def test_unknown_format_exits_with_usage_code_and_lists_the_known_ones(db_url):
    result = runner.invoke(app, ingest_args(db_url, **{"--format": "cypress"}))
    assert result.exit_code == EXIT_USAGE
    assert "junit" in result.output and "allure" in result.output


def test_unparseable_report_exits_with_parse_code(db_url):
    result = runner.invoke(
        app,
        ingest_args(db_url, **{"--path": str(FIXTURES / "malformed" / "truncated.xml")}),
    )
    assert result.exit_code == EXIT_PARSE_ERROR
    assert "Could not parse report" in result.output


def test_reingesting_exits_with_duplicate_code_not_a_generic_failure(db_url):
    # A pipeline needs to tell "already ingested" apart from "the report was
    # broken", because the first is frequently fine and the second never is.
    assert runner.invoke(app, ingest_args(db_url)).exit_code == 0
    second = runner.invoke(app, ingest_args(db_url))
    assert second.exit_code == EXIT_DUPLICATE
    assert "replace=True" in second.output


def test_replace_flag_makes_reingest_succeed(db_url):
    runner.invoke(app, ingest_args(db_url))
    result = runner.invoke(app, [*ingest_args(db_url), "--replace"])
    assert result.exit_code == 0, result.output
    assert "Replaced run 1" in result.output

    engine = create_db_engine(db_url)
    with session_scope(engine) as session:
        assert len(session.execute(select(TestRunRow)).scalars().all()) == 1
        assert len(session.execute(select(TestResultRow)).scalars().all()) == 9
    engine.dispose()


def test_missing_commit_warns_but_still_ingests(db_url):
    # Ingesting a local run without a SHA is legitimate; silently degrading the
    # data it feeds is not, so this warns rather than failing.
    result = runner.invoke(app, ingest_args(db_url, **{"--commit": None}))
    assert result.exit_code == 0
    assert "no --commit given" in result.output
    assert "same-commit flake detection cannot use it" in result.output


def test_ingests_an_allure_directory(db_url):
    result = runner.invoke(
        app,
        ingest_args(
            db_url,
            **{
                "--path": str(FIXTURES / "allure" / "playwright-producer"),
                "--format": "allure",
                "--suite": "orangehrm-e2e",
            },
        ),
    )
    assert result.exit_code == 0, result.output
    assert "4 results" in result.output


def test_formats_command_lists_every_registered_parser():
    result = runner.invoke(app, ["formats"])
    assert result.exit_code == 0
    assert set(result.output.split()) == {"allure", "junit", "playwright", "pytest-json"}


def test_info_reports_the_target_database(db_url):
    result = runner.invoke(app, ["info", "--database-url", db_url])
    assert result.exit_code == 0
    assert db_url in result.output


# --------------------------------------------------------------------------
# Phase 2 commands
# --------------------------------------------------------------------------


def seed_flaky_suite(db_url):
    """One alternating test and one solid test, sharing eight runs."""
    from datetime import UTC, datetime, timedelta

    from testpulse_core.models import TestResult, TestRun, TestStatus
    from testpulse_core.storage.repository import store_run

    start = datetime(2026, 7, 1, tzinfo=UTC)
    engine = create_db_engine(db_url)
    patterns = {"tests/a.py::Cls::unstable": "PFPFPFPF", "tests/a.py::Cls::solid": "PPPPPPPP"}
    for index in range(8):
        run = TestRun(
            suite_name="admin-e2e",
            started_at=start + timedelta(hours=index),
            finished_at=start + timedelta(hours=index, minutes=1),
            source_format="junit",
            commit_sha=f"sha{index:04d}",
            branch="main",
            environment="ci",
            results=[
                TestResult(
                    test_id=test_id,
                    display_name=test_id.rsplit("::", 1)[-1],
                    status=(
                        TestStatus.PASSED if pattern[index] == "P" else TestStatus.FAILED
                    ),
                    duration_ms=100,
                    raw_status=pattern[index],
                )
                for test_id, pattern in patterns.items()
            ],
        )
        with session_scope(engine) as session:
            store_run(session, run)
    engine.dispose()


def test_suites_command_lists_what_is_stored(db_url):
    runner.invoke(app, ingest_args(db_url))
    result = runner.invoke(app, ["suites", "--database-url", db_url])
    assert result.exit_code == 0
    assert "sample-suite" in result.output


def test_metrics_reports_per_test_health(db_url):
    seed_flaky_suite(db_url)
    result = runner.invoke(app, ["metrics", "--suite", "admin-e2e", "--database-url", db_url])
    assert result.exit_code == 0, result.output
    assert "unstable" in result.output
    assert "2 tests, 1 flaky" in result.output


def test_metrics_on_an_unknown_suite_exits_with_usage_code(db_url):
    result = runner.invoke(app, ["metrics", "--suite", "nope", "--database-url", db_url])
    assert result.exit_code == EXIT_USAGE
    assert "No runs stored" in result.output


def test_flaky_command_names_the_evidence(db_url):
    seed_flaky_suite(db_url)
    result = runner.invoke(app, ["flaky", "--suite", "admin-e2e", "--database-url", db_url])
    assert result.exit_code == 0
    assert "evidence=rolling-flip" in result.output
    assert "unstable" in result.output
    assert "solid" not in result.output


def test_flaky_can_gate_a_pipeline(db_url):
    # Distinct exit code: a suite with flaky tests still ran, and a step that
    # cannot tell that from a broken build has to treat both as fatal.
    seed_flaky_suite(db_url)
    result = runner.invoke(
        app, ["flaky", "--suite", "admin-e2e", "--database-url", db_url, "--fail-on-flaky"]
    )
    assert result.exit_code == EXIT_FLAKY_FOUND


def test_flaky_gate_passes_when_there_is_nothing_flaky(db_url):
    runner.invoke(app, ingest_args(db_url))
    result = runner.invoke(
        app, ["flaky", "--suite", "sample-suite", "--database-url", db_url, "--fail-on-flaky"]
    )
    assert result.exit_code == 0
    assert "No flaky tests" in result.output


def test_quarantine_add_reports_the_expiry_date(db_url):
    result = runner.invoke(
        app,
        [
            "quarantine", "add",
            "--suite", "admin-e2e",
            "--test-id", "tests/a.py::Cls::unstable",
            "--reason", "times out on slow runners",
            "--by", "mohanad",
            "--days", "14",
            "--database-url", db_url,
        ],
    )
    assert result.exit_code == 0, result.output
    assert "14 days" in result.output


def test_quarantine_list_shows_the_reason(db_url):
    runner.invoke(app, ["quarantine", "add", "--suite", "s", "--test-id", "a.py::C::t",
                        "--reason", "flaky on CI", "--database-url", db_url])
    result = runner.invoke(app, ["quarantine", "list", "--suite", "s", "--database-url", db_url])
    assert result.exit_code == 0
    assert "flaky on CI" in result.output
    assert "none overdue" in result.output


def test_quarantine_list_json_is_machine_readable(db_url):
    import json as json_module

    runner.invoke(app, ["quarantine", "add", "--suite", "s", "--test-id", "a.py::C::t",
                        "--database-url", db_url])
    result = runner.invoke(
        app, ["quarantine", "list", "--suite", "s", "--format", "json", "--database-url", db_url]
    )
    payload = json_module.loads(result.output)
    assert payload[0]["test_id"] == "a.py::C::t"
    assert payload[0]["expired"] is False


def test_quarantine_list_emits_pytest_deselect_arguments(db_url):
    runner.invoke(app, ["quarantine", "add", "--suite", "s", "--test-id", "a.py::C::t",
                        "--database-url", db_url])
    result = runner.invoke(
        app,
        ["quarantine", "list", "--suite", "s", "--format", "pytest-deselect",
         "--database-url", db_url],
    )
    assert result.output.strip() == "--deselect a.py::C::t"


def test_quarantine_list_emits_a_playwright_grep_pattern(db_url):
    runner.invoke(app, ["quarantine", "add", "--suite", "s", "--test-id",
                        "a.spec.ts::Suite::books a slot [Cairo]", "--database-url", db_url])
    result = runner.invoke(
        app,
        ["quarantine", "list", "--suite", "s", "--format", "playwright-grep",
         "--database-url", db_url],
    )
    assert result.output.strip() == r"books\ a\ slot\ \[Cairo\]"


def test_unknown_quarantine_format_is_a_usage_error(db_url):
    result = runner.invoke(
        app, ["quarantine", "list", "--suite", "s", "--format", "yaml", "--database-url", db_url]
    )
    assert result.exit_code == EXIT_USAGE


def test_removing_a_test_that_is_not_quarantined_is_a_usage_error(db_url):
    result = runner.invoke(
        app, ["quarantine", "remove", "--suite", "s", "--test-id", "nope", "--database-url", db_url]
    )
    assert result.exit_code == EXIT_USAGE
    assert "not quarantined" in result.output
