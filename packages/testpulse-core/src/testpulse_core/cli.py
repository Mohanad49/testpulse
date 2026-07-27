"""Command line interface.

Designed to be called from a CI step, which drives two choices: every option
maps to something a CI runner already has in its environment, and exit codes are
meaningful because that is the only thing a pipeline reads reliably.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Annotated

import typer

from testpulse_core.config import get_settings
from testpulse_core.models import RunMetadata
from testpulse_core.parsers import (
    ParseError,
    UnknownFormatError,
    available_formats,
    get_parser,
)
from testpulse_core.storage.db import create_db_engine, session_scope
from testpulse_core.storage.repository import DuplicateRunError, store_run

app = typer.Typer(
    help="Test observability and flake detection for CI test suites.",
    no_args_is_help=True,
    add_completion=False,
)

# Exit codes. A CI step needs to tell "the report was bad" apart from "this was
# already ingested" without parsing stderr, because the second is often fine.
EXIT_PARSE_ERROR = 2
EXIT_DUPLICATE = 3
EXIT_USAGE = 4


@app.command()
def ingest(
    path: Annotated[
        Path,
        typer.Option("--path", help="Report file, or results directory for Allure."),
    ],
    suite: Annotated[
        str,
        typer.Option("--suite", help="Suite name. Groups runs; keep it stable across runs."),
    ],
    format_: Annotated[
        str,
        typer.Option("--format", help=f"One of: {', '.join(available_formats())}."),
    ],
    commit: Annotated[
        str | None,
        typer.Option("--commit", help="Commit SHA, e.g. $GITHUB_SHA."),
    ] = None,
    branch: Annotated[
        str | None,
        typer.Option("--branch", help="Branch name, e.g. $GITHUB_REF_NAME."),
    ] = None,
    env: Annotated[
        str | None,
        typer.Option("--env", help='Environment label, e.g. "chrome-ci".'),
    ] = None,
    ci_run_url: Annotated[
        str | None,
        typer.Option("--ci-run-url", help="Link back to the CI run."),
    ] = None,
    database_url: Annotated[
        str | None,
        typer.Option("--database-url", help="Overrides TESTPULSE_DATABASE_URL."),
    ] = None,
    replace: Annotated[
        bool,
        typer.Option("--replace", help="Overwrite a previously ingested identical run."),
    ] = False,
) -> None:
    """Parse a test report and store it.

    Without ``--commit`` the run is stored but can never participate in
    same-commit flake detection, so a warning is emitted rather than failing:
    ingesting a local run without a SHA is legitimate, silently degrading the
    data it feeds is not.
    """
    try:
        parser = get_parser(format_)
    except UnknownFormatError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(EXIT_USAGE) from exc

    if commit is None:
        typer.secho(
            "Warning: no --commit given. This run will be stored, but same-commit "
            "flake detection cannot use it.",
            fg=typer.colors.YELLOW,
            err=True,
        )

    meta = RunMetadata(
        suite_name=suite,
        commit_sha=commit,
        branch=branch,
        ci_run_url=ci_run_url,
        environment=env,
    )

    try:
        run = parser.parse(path, meta)
    except ParseError as exc:
        typer.secho(f"Could not parse report: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(EXIT_PARSE_ERROR) from exc

    for warning in run.warnings:
        typer.secho(f"Warning: {warning}", fg=typer.colors.YELLOW, err=True)

    engine = create_db_engine(database_url)
    try:
        with session_scope(engine) as session:
            summary = store_run(session, run, replace=replace)
    except DuplicateRunError as exc:
        typer.secho(str(exc), fg=typer.colors.YELLOW, err=True)
        raise typer.Exit(EXIT_DUPLICATE) from exc
    finally:
        engine.dispose()

    if summary.replaced_id is not None:
        typer.echo(f"Replaced run {summary.replaced_id}.")
    typer.echo(
        f"Ingested run {summary.run_id}: {summary.results_written} results "
        f"from {suite!r} ({run.passed} passed, {run.failed} failed, "
        f"{run.errored} errored, {run.skipped} skipped)."
    )


@app.command()
def formats() -> None:
    """List the report formats this build can ingest."""
    for name in available_formats():
        typer.echo(name)


@app.command()
def info(
    database_url: Annotated[
        str | None,
        typer.Option("--database-url", help="Overrides TESTPULSE_DATABASE_URL."),
    ] = None,
) -> None:
    """Show which database the CLI would write to.

    Exists because "it ran but I see nothing" is nearly always two different
    database URLs, and that is tedious to diagnose without a way to ask.
    """
    url = database_url or get_settings().database_url
    typer.echo(url)


def main() -> None:  # pragma: no cover - thin wrapper
    sys.exit(app())


if __name__ == "__main__":  # pragma: no cover
    main()
