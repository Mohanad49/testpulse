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

from testpulse_core import quarantine as quarantine_service
from testpulse_core.config import get_settings
from testpulse_core.metrics import TestMetrics
from testpulse_core.models import RunMetadata
from testpulse_core.parsers import (
    ParseError,
    UnknownFormatError,
    available_formats,
    get_parser,
)
from testpulse_core.report import render_markdown
from testpulse_core.storage.db import create_db_engine, session_scope
from testpulse_core.storage.queries import list_suites, run_report, suite_metrics
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
EXIT_FLAKY_FOUND = 5
"""`flaky --fail-on-flaky` uses this so a pipeline can gate on it. Separate from
a generic failure because a suite with flaky tests still ran, and a step that
cannot tell those apart has to treat both as a broken build."""

quarantine_app = typer.Typer(help="Manage quarantined tests.", no_args_is_help=True)
app.add_typer(quarantine_app, name="quarantine")


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


@app.command()
def suites(
    database_url: Annotated[str | None, typer.Option("--database-url")] = None,
) -> None:
    """List suites that have runs stored."""
    engine = create_db_engine(database_url)
    try:
        with session_scope(engine) as session:
            for name in list_suites(session):
                typer.echo(name)
    finally:
        engine.dispose()


def _load_metrics(
    suite: str,
    database_url: str | None,
    branch: str | None,
    window: int | None,
) -> list[TestMetrics]:
    settings = get_settings()
    flake_config = settings.flake
    if window is not None:
        flake_config = flake_config.model_copy(update={"window_size": window})

    engine = create_db_engine(database_url)
    try:
        with session_scope(engine) as session:
            return suite_metrics(
                session,
                suite,
                flake_config,
                settings.newly_failing,
                branch=branch,
            )
    finally:
        engine.dispose()


@app.command()
def metrics(
    suite: Annotated[str, typer.Option("--suite")],
    database_url: Annotated[str | None, typer.Option("--database-url")] = None,
    branch: Annotated[
        str | None,
        typer.Option("--branch", help="Only count runs from this branch."),
    ] = None,
    window: Annotated[
        int | None,
        typer.Option("--window", help="Override the configured window size."),
    ] = None,
    limit: Annotated[int, typer.Option("--limit")] = 20,
) -> None:
    """Show per-test health for a suite, flakiest first."""
    computed = _load_metrics(suite, database_url, branch, window)
    if not computed:
        typer.secho(f"No runs stored for suite {suite!r}.", fg=typer.colors.YELLOW, err=True)
        raise typer.Exit(EXIT_USAGE)

    header = f"{'test':<52} {'runs':>5} {'pass':>6} {'flip':>6} {'score':>6} {'p95ms':>8}"
    typer.echo(header)
    typer.echo("-" * len(header))
    for metric in computed[:limit]:
        rate = "n/a" if metric.pass_rate is None else f"{metric.pass_rate:.0%}"
        name = metric.display_name[:50]
        typer.echo(
            f"{name:<52} {metric.scored_runs:>5} {rate:>6} "
            f"{metric.flip_rate:>6.2f} {metric.flakiness_score:>6.2f} "
            f"{metric.p95_duration_ms:>8}"
        )

    flaky = [m for m in computed if m.is_flaky]
    newly_failing = [m for m in computed if m.is_newly_failing]
    typer.echo(
        f"\n{len(computed)} tests, {len(flaky)} flaky, {len(newly_failing)} newly failing."
    )


@app.command()
def flaky(
    suite: Annotated[str, typer.Option("--suite")],
    database_url: Annotated[str | None, typer.Option("--database-url")] = None,
    branch: Annotated[str | None, typer.Option("--branch")] = None,
    window: Annotated[int | None, typer.Option("--window")] = None,
    fail_on_flaky: Annotated[
        bool,
        typer.Option("--fail-on-flaky", help=f"Exit {EXIT_FLAKY_FOUND} if any are found."),
    ] = False,
) -> None:
    """List the tests the classifier considers flaky, and why."""
    computed = _load_metrics(suite, database_url, branch, window)
    found = [m for m in computed if m.is_flaky]

    if not found:
        typer.echo(f"No flaky tests in {suite!r}.")
        return

    for metric in found:
        rate = "n/a" if metric.pass_rate is None else f"{metric.pass_rate:.0%}"
        typer.echo(
            f"{metric.flakiness_score:.2f}  {metric.display_name}\n"
            f"        evidence={','.join(metric.flake_evidence)} "
            f"pass={rate} flip={metric.flip_rate:.2f} runs={metric.scored_runs}\n"
            f"        id={metric.test_id}"
        )

    typer.echo(f"\n{len(found)} flaky test(s).")
    if fail_on_flaky:
        raise typer.Exit(EXIT_FLAKY_FOUND)


@app.command()
def report(
    suite: Annotated[str, typer.Option("--suite")],
    run_id: Annotated[
        int | None,
        typer.Option("--run-id", help="Defaults to the newest run for the suite."),
    ] = None,
    branch: Annotated[str | None, typer.Option("--branch")] = None,
    dashboard_url: Annotated[
        str | None,
        typer.Option("--dashboard-url", help="Linked from the comment."),
    ] = None,
    output: Annotated[
        Path | None,
        typer.Option("--output", help="Write markdown here instead of stdout."),
    ] = None,
    fail_on_new: Annotated[
        bool,
        typer.Option("--fail-on-new", help=f"Exit {EXIT_FLAKY_FOUND} if anything got worse."),
    ] = False,
    database_url: Annotated[str | None, typer.Option("--database-url")] = None,
) -> None:
    """Summarise what changed in a run, as markdown for a PR comment.

    Reports changes rather than state. "47 tests failed" is useless in a pull
    request because 45 of them were already failing on main; "2 tests started
    failing" is the only line that decides anything.
    """
    settings = get_settings()
    engine = create_db_engine(database_url)
    try:
        with session_scope(engine) as session:
            built = run_report(
                session,
                suite,
                settings.flake,
                settings.newly_failing,
                settings.report,
                run_id=run_id,
                branch=branch,
            )
    finally:
        engine.dispose()

    if built is None:
        # Distinct from "nothing changed". A comment claiming nothing broke when
        # nothing was ingested at all would be a lie.
        typer.secho(
            f"No runs stored for suite {suite!r}, so there is nothing to compare.",
            fg=typer.colors.YELLOW,
            err=True,
        )
        raise typer.Exit(EXIT_USAGE)

    markdown = render_markdown(
        built, dashboard_url=dashboard_url, limit=settings.report.max_items_per_section
    )
    if output:
        output.write_text(markdown)
        typer.secho(f"Wrote {output}", fg=typer.colors.GREEN, err=True)
    else:
        typer.echo(markdown)

    if fail_on_new and built.has_bad_news:
        raise typer.Exit(EXIT_FLAKY_FOUND)


@quarantine_app.command("add")
def quarantine_add(
    suite: Annotated[str, typer.Option("--suite")],
    test_id: Annotated[str, typer.Option("--test-id")],
    reason: Annotated[str | None, typer.Option("--reason")] = None,
    by: Annotated[str | None, typer.Option("--by", help="Who made the call.")] = None,
    days: Annotated[
        int | None,
        typer.Option("--days", help="Overrides the configured expiry."),
    ] = None,
    database_url: Annotated[str | None, typer.Option("--database-url")] = None,
) -> None:
    """Quarantine a test, or reset the clock on one already quarantined."""
    settings = get_settings()
    engine = create_db_engine(database_url)
    try:
        with session_scope(engine) as session:
            entry = quarantine_service.add(
                session,
                suite,
                test_id,
                expires_after_days=days or settings.quarantine.default_expires_after_days,
                reason=reason,
                quarantined_by=by,
            )
    finally:
        engine.dispose()
    typer.echo(
        f"Quarantined {entry.test_id} in {entry.suite_name} "
        f"until {entry.expires_at.date().isoformat()} ({entry.expires_after_days} days)."
    )


@quarantine_app.command("remove")
def quarantine_remove(
    suite: Annotated[str, typer.Option("--suite")],
    test_id: Annotated[str, typer.Option("--test-id")],
    database_url: Annotated[str | None, typer.Option("--database-url")] = None,
) -> None:
    """Take a test out of quarantine."""
    engine = create_db_engine(database_url)
    try:
        with session_scope(engine) as session:
            quarantine_service.remove(session, suite, test_id)
    except quarantine_service.QuarantineError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(EXIT_USAGE) from exc
    finally:
        engine.dispose()
    typer.echo(f"Removed {test_id} from quarantine in {suite}.")


@quarantine_app.command("list")
def quarantine_list(
    suite: Annotated[str, typer.Option("--suite")],
    output_format: Annotated[
        str,
        typer.Option("--format", help="table, json, pytest-deselect, or playwright-grep."),
    ] = "table",
    database_url: Annotated[str | None, typer.Option("--database-url")] = None,
) -> None:
    """Show quarantined tests, most overdue first.

    The machine-readable formats exist so a CI step can act on the list without
    parsing prose. Expired entries are still included in them: expiry marks debt
    for a human to resolve, it does not silently re-enable a test that nobody has
    looked at.
    """
    engine = create_db_engine(database_url)
    try:
        with session_scope(engine) as session:
            entries = quarantine_service.list_entries(session, suite)
    finally:
        engine.dispose()

    if output_format == "json":
        typer.echo(quarantine_service.as_json(entries))
        return
    if output_format == "pytest-deselect":
        typer.echo(quarantine_service.as_pytest_deselect(entries))
        return
    if output_format == "playwright-grep":
        typer.echo(quarantine_service.as_playwright_grep(entries))
        return
    if output_format != "table":
        typer.secho(
            f"Unknown format {output_format!r}. "
            "Use table, json, pytest-deselect, or playwright-grep.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(EXIT_USAGE)

    if not entries:
        typer.echo(f"Nothing quarantined in {suite!r}.")
        return

    for entry in entries:
        if entry.is_expired:
            marker = typer.style(f"EXPIRED {-entry.days_remaining}d ago", fg=typer.colors.RED)
        else:
            marker = f"{entry.days_remaining}d left"
        typer.echo(f"{marker:<24} {entry.test_id}")
        if entry.reason:
            typer.echo(f"{'':<24} reason: {entry.reason}")

    overdue = quarantine_service.debt(entries)
    if overdue:
        typer.secho(
            f"\nQuarantine debt: {len(overdue)} of {len(entries)} entries are past "
            "their expiry and need a decision.",
            fg=typer.colors.RED,
        )
    else:
        typer.echo(f"\n{len(entries)} quarantined, none overdue.")


def main() -> None:  # pragma: no cover - thin wrapper
    sys.exit(app())


if __name__ == "__main__":  # pragma: no cover
    main()
