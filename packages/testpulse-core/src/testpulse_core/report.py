"""What changed in this run, for a PR comment.

A PR comment is read in about four seconds by someone who wants to know whether
to merge. That constraint drives everything here: it reports *changes*, not
state. "47 tests failed" is state and is useless in a PR, because 45 of them were
already failing on main and have nothing to do with this change. "2 tests started
failing" is a change and is the only line that matters.

Everything is computed by comparing the newest run against the window of runs
before it, never against the window including it. Including the run being
reported on lets it dilute its own signal: a test that failed once in a window of
one has a pass rate of 0, and a test that failed once in a window of fifty looks
almost fine.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from testpulse_core.config import FlakeConfig, NewlyFailingConfig, ReportConfig
from testpulse_core.metrics import Observation, classify, p95_duration_ms, pass_rate
from testpulse_core.models import TestStatus


@dataclass(frozen=True, slots=True)
class ChangedTest:
    test_id: str
    display_name: str
    detail: str = ""


@dataclass(frozen=True, slots=True)
class DurationRegression:
    test_id: str
    display_name: str
    current_ms: int
    baseline_ms: int

    @property
    def factor(self) -> float:
        return self.current_ms / self.baseline_ms if self.baseline_ms else 0.0


@dataclass(frozen=True, slots=True)
class RunReport:
    """The diff between the newest run and everything before it."""

    suite_name: str
    run_id: int
    commit_sha: str | None
    total: int
    passed: int
    failed: int
    skipped: int
    errored: int

    new_failures: list[ChangedTest] = field(default_factory=list)
    """Tests failing now that were passing in the prior window. The headline."""

    newly_flaky: list[ChangedTest] = field(default_factory=list)
    """Tests the classifier flags now and did not flag before this run."""

    recovered: list[ChangedTest] = field(default_factory=list)
    """Tests that were failing before and pass now. Reported because a PR comment
    that only ever brings bad news gets muted, and because a test recovering with
    no corresponding fix is itself a flake signal."""

    still_failing: list[ChangedTest] = field(default_factory=list)
    """Pre-existing failures, counted but not listed. They are not this PR's
    fault and listing them buries the ones that are."""

    duration_regressions: list[DurationRegression] = field(default_factory=list)

    first_seen: list[ChangedTest] = field(default_factory=list)
    """Tests appearing for the first time. A new test that fails immediately is
    not a regression, and calling it one sends someone hunting for a break that
    never happened."""

    @property
    def has_bad_news(self) -> bool:
        return bool(self.new_failures or self.newly_flaky or self.duration_regressions)


def build_report(
    suite_name: str,
    run_id: int,
    commit_sha: str | None,
    current: dict[str, Observation],
    history: dict[str, list[Observation]],
    display_names: dict[str, str],
    flake_config: FlakeConfig,
    newly_failing_config: NewlyFailingConfig,
    report_config: ReportConfig,
) -> RunReport:
    """Diff one run against the history that preceded it.

    ``current`` is this run's result per test. ``history`` is every prior
    observation per test, excluding this run.
    """
    new_failures: list[ChangedTest] = []
    newly_flaky: list[ChangedTest] = []
    recovered: list[ChangedTest] = []
    still_failing: list[ChangedTest] = []
    regressions: list[DurationRegression] = []
    first_seen: list[ChangedTest] = []

    for test_id, observation in current.items():
        name = display_names.get(test_id, test_id)
        prior = history.get(test_id, [])

        if not prior:
            # No history at all. Whatever it did, it did not change.
            if observation.is_failure:
                first_seen.append(
                    ChangedTest(test_id, name, "new test, failing on its first run")
                )
            continue

        prior_rate = pass_rate(prior)
        # "Was it failing" means the last time it actually ran, not the last row
        # in the list. A skip in between is not a recovery, and using prior[-1]
        # directly reported a test that had failed its last eight real runs as a
        # brand new failure because the most recent entry happened to be a skip.
        last_scored = next((o for o in reversed(prior) if o.status is not TestStatus.SKIPPED), None)
        was_failing = last_scored is not None and last_scored.is_failure

        if observation.is_failure:
            if was_failing:
                still_failing.append(ChangedTest(test_id, name))
            elif last_scored is not None and prior_rate is not None:
                # Passed the last time it ran, fails now. This is the line that
                # decides whether someone blocks the merge.
                scored_count = sum(1 for o in prior if o.status is not TestStatus.SKIPPED)
                new_failures.append(
                    ChangedTest(
                        test_id,
                        name,
                        f"passed {int(prior_rate * 100)}% of its last {scored_count} runs",
                    )
                )
        elif was_failing:
            recovered.append(ChangedTest(test_id, name, "was failing, passes now"))

        # Flakiness is judged on the full picture including this run, then
        # compared against the same judgement made without it. That is the only
        # way to say "newly" flaky rather than just "flaky".
        was_flaky, _ = classify(prior, flake_config)
        is_flaky, evidence = classify([*prior, observation], flake_config)
        if is_flaky and not was_flaky:
            newly_flaky.append(ChangedTest(test_id, name, f"evidence: {', '.join(evidence)}"))

        if observation.status is not TestStatus.SKIPPED:
            baseline = p95_duration_ms(prior)
            # The floor matters more than the ratio. Without it a test going from
            # 2ms to 6ms is a "200% regression", and the report fills up with
            # noise from the fastest tests in the suite.
            if (
                baseline >= report_config.duration_floor_ms
                and observation.duration_ms
                > baseline * (1 + report_config.duration_regression_pct / 100)
            ):
                regressions.append(
                    DurationRegression(test_id, name, observation.duration_ms, baseline)
                )

    statuses = [o.status for o in current.values()]
    report = RunReport(
        suite_name=suite_name,
        run_id=run_id,
        commit_sha=commit_sha,
        total=len(current),
        passed=sum(1 for s in statuses if s is TestStatus.PASSED),
        failed=sum(1 for s in statuses if s is TestStatus.FAILED),
        skipped=sum(1 for s in statuses if s is TestStatus.SKIPPED),
        errored=sum(1 for s in statuses if s is TestStatus.ERROR),
        new_failures=sorted(new_failures, key=lambda t: t.display_name),
        newly_flaky=sorted(newly_flaky, key=lambda t: t.display_name),
        recovered=sorted(recovered, key=lambda t: t.display_name),
        still_failing=sorted(still_failing, key=lambda t: t.display_name),
        duration_regressions=sorted(regressions, key=lambda r: -r.factor),
        first_seen=sorted(first_seen, key=lambda t: t.display_name),
    )
    return report


def _section(title: str, items: list[ChangedTest], limit: int) -> list[str]:
    if not items:
        return []
    lines = [f"**{title}**", ""]
    for item in items[:limit]:
        suffix = f" — {item.detail}" if item.detail else ""
        lines.append(f"- `{item.display_name}`{suffix}")
    if len(items) > limit:
        lines.append(f"- …and {len(items) - limit} more")
    lines.append("")
    return lines


def render_markdown(
    report: RunReport,
    dashboard_url: str | None = None,
    limit: int = 10,
) -> str:
    """Render the report as a PR comment.

    Ordered by what a reviewer needs first: the verdict, then what broke, then
    what got better, then the noise. Long lists are truncated rather than folded
    into a details block, because a comment that needs a click to reveal bad news
    is a comment that hides bad news.
    """
    lines: list[str] = ["## TestPulse", ""]

    if report.has_bad_news:
        headline = []
        if report.new_failures:
            headline.append(f"{len(report.new_failures)} newly failing")
        if report.newly_flaky:
            headline.append(f"{len(report.newly_flaky)} newly flaky")
        if report.duration_regressions:
            headline.append(f"{len(report.duration_regressions)} slower")
        lines.append(f"⚠️ **{', '.join(headline)}** in `{report.suite_name}`.")
    else:
        lines.append(f"✅ Nothing new broke in `{report.suite_name}`.")
    lines.append("")

    lines.append(
        f"`{report.total}` tests · {report.passed} passed · {report.failed} failed · "
        f"{report.errored} errored · {report.skipped} skipped"
    )
    if report.still_failing:
        # Counted, never listed. These are not this change's fault and listing
        # them buries the ones that are.
        lines.append(
            f"\n<sub>{len(report.still_failing)} test(s) were already failing before "
            "this run and are not listed.</sub>"
        )
    lines.append("")

    lines += _section("🔴 Started failing", report.new_failures, limit)
    lines += _section("🟠 Newly flaky", report.newly_flaky, limit)
    lines += _section("🆕 New tests failing on first run", report.first_seen, limit)

    if report.duration_regressions:
        lines += ["**🐌 Slower**", ""]
        for regression in report.duration_regressions[:limit]:
            lines.append(
                f"- `{regression.display_name}` — {regression.baseline_ms}ms → "
                f"{regression.current_ms}ms ({regression.factor:.1f}x slower)"
            )
        if len(report.duration_regressions) > limit:
            lines.append(f"- …and {len(report.duration_regressions) - limit} more")
        lines.append("")

    lines += _section("🟢 Recovered", report.recovered, limit)

    if dashboard_url:
        lines.append(f"[Open in TestPulse]({dashboard_url})")
        lines.append("")

    if report.commit_sha:
        lines.append(f"<sub>Run {report.run_id} · `{report.commit_sha[:7]}`</sub>")

    return "\n".join(lines).strip() + "\n"
