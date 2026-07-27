"""CLI tests.

The CLI is the surface CI actually calls, so the assertions here are about exit
codes and stderr: those are the only two things a pipeline step reads reliably.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from typer.testing import CliRunner

from conftest import FIXTURES
from testpulse_core.cli import EXIT_DUPLICATE, EXIT_PARSE_ERROR, EXIT_USAGE, app
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
