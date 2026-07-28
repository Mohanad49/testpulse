"""Run-diff report tests.

Every case here is about the distinction the report exists to draw: what
*changed* versus what was already true. Getting that wrong in either direction
makes the comment useless — reporting pre-existing failures makes every PR look
broken, and missing a new one makes the comment worthless.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from testpulse_core.config import FlakeConfig, NewlyFailingConfig, ReportConfig
from testpulse_core.metrics import Observation
from testpulse_core.models import TestStatus
from testpulse_core.report import build_report, render_markdown

START = datetime(2026, 7, 1, tzinfo=UTC)
FLAKE = FlakeConfig()
NEWLY = NewlyFailingConfig()
REPORT = ReportConfig()

STATUSES = {
    "P": TestStatus.PASSED,
    "F": TestStatus.FAILED,
    "E": TestStatus.ERROR,
    "S": TestStatus.SKIPPED,
}


def history(pattern: str, *, commit: str | None = None, duration: int = 100):
    return [
        Observation(
            run_id=index + 1,
            started_at=START + timedelta(hours=index),
            status=STATUSES[char],
            duration_ms=duration,
            commit_sha=commit or f"sha{index}",
        )
        for index, char in enumerate(pattern)
    ]


def current(char: str, *, duration: int = 100, commit: str = "head", retry: int | None = None):
    return Observation(
        run_id=999,
        started_at=START + timedelta(days=1),
        status=STATUSES[char],
        duration_ms=duration,
        commit_sha=commit,
        retry_count=retry,
    )


def report_for(current_map, history_map):
    return build_report(
        suite_name="admin-e2e",
        run_id=999,
        commit_sha="head1234",
        current=current_map,
        history=history_map,
        display_names={k: k.rsplit("::", 1)[-1] for k in current_map},
        flake_config=FLAKE,
        newly_failing_config=NEWLY,
        report_config=REPORT,
    )


def test_a_test_that_was_passing_and_now_fails_is_a_new_failure():
    report = report_for({"a::b::login": current("F")}, {"a::b::login": history("PPPPP")})
    assert [t.display_name for t in report.new_failures] == ["login"]
    assert report.still_failing == []


def test_a_test_that_was_already_failing_is_not_a_new_failure():
    # The single most important behaviour. Reporting these makes every PR look
    # broken and the comment gets ignored within a week.
    report = report_for({"a::b::login": current("F")}, {"a::b::login": history("PPPFF")})
    assert report.new_failures == []
    assert [t.display_name for t in report.still_failing] == ["login"]


def test_pre_existing_failures_are_counted_but_not_listed_in_the_comment():
    report = report_for(
        {f"a::b::t{i}": current("F") for i in range(5)},
        {f"a::b::t{i}": history("PPFF") for i in range(5)},
    )
    markdown = render_markdown(report)
    assert "5 test(s) were already failing" in markdown
    assert "Started failing" not in markdown


def test_a_test_that_recovers_is_reported():
    # A comment that only ever brings bad news gets muted.
    report = report_for({"a::b::login": current("P")}, {"a::b::login": history("PPFF")})
    assert [t.display_name for t in report.recovered] == ["login"]


def test_a_brand_new_failing_test_is_not_called_a_regression():
    # Nothing regressed; the test did not exist before. Calling it a regression
    # sends someone looking for a break that never happened.
    report = report_for({"a::b::brand_new": current("F")}, {})
    assert report.new_failures == []
    assert [t.display_name for t in report.first_seen] == ["brand_new"]


def test_flaky_before_and_flaky_now_is_not_news():
    already = report_for({"a::b::t": current("F")}, {"a::b::t": history("PFPFPFPF")})
    assert already.newly_flaky == []


def test_a_recovery_can_be_what_makes_a_test_newly_flaky():
    # A realistic transition. Five passes then four failures reads as a clean
    # regression: flip rate 0.125, below the gate, so not flaky. Then it passes
    # again with nothing fixed. That second flip pushes the flip rate to 0.22 and
    # the same test is now flaky rather than broken - which is exactly the
    # reclassification a reviewer needs told, because the two need opposite
    # responses.
    report = report_for({"a::b::t": current("P")}, {"a::b::t": history("PPPPPFFFF")})
    assert [t.display_name for t in report.newly_flaky] == ["t"]
    assert "rolling-flip" in report.newly_flaky[0].detail
    # And it is reported as recovered in the same comment, which is the honest
    # pair of facts: it passes now, and do not trust that.
    assert [t.display_name for t in report.recovered] == ["t"]


def test_a_retried_pass_makes_a_test_newly_flaky():
    # Same-commit evidence from a single run, which is the phase 1 retry_count
    # decision paying off inside the PR comment.
    report = report_for(
        {"a::b::t": current("P", retry=1)}, {"a::b::t": history("PPPPP")}
    )
    assert [t.display_name for t in report.newly_flaky] == ["t"]
    assert "same-commit" in report.newly_flaky[0].detail


def test_duration_regression_needs_to_clear_both_gates():
    slow_history = {"a::b::t": history("PPPPP", duration=1000)}
    # 1000ms baseline, 2000ms now: over the 50% threshold and over the floor.
    assert report_for({"a::b::t": current("P", duration=2000)}, slow_history).duration_regressions

    # Same 2x ratio, but 4ms to 8ms. Below the floor, so not reported.
    fast_history = {"a::b::t": history("PPPPP", duration=4)}
    assert not report_for(
        {"a::b::t": current("P", duration=8)}, fast_history
    ).duration_regressions


def test_a_small_slowdown_over_the_floor_is_not_a_regression():
    # 1000ms -> 1200ms is 20%, under the 50% gate. CI runners vary this much
    # between jobs and firing here would train people to ignore the comment.
    assert not report_for(
        {"a::b::t": current("P", duration=1200)},
        {"a::b::t": history("PPPPP", duration=1000)},
    ).duration_regressions


def test_skipped_tests_never_produce_duration_regressions():
    assert not report_for(
        {"a::b::t": current("S", duration=99999)},
        {"a::b::t": history("PPPPP", duration=100)},
    ).duration_regressions


def test_a_clean_run_says_so_clearly():
    report = report_for({"a::b::t": current("P")}, {"a::b::t": history("PPPPP")})
    assert report.has_bad_news is False
    markdown = render_markdown(report)
    assert "Nothing new broke" in markdown
    assert "Started failing" not in markdown


def test_the_headline_summarises_every_kind_of_bad_news():
    report = report_for(
        {
            "a::b::broke": current("F"),
            "a::b::slow": current("P", duration=5000),
        },
        {
            "a::b::broke": history("PPPPP"),
            "a::b::slow": history("PPPPP", duration=1000),
        },
    )
    markdown = render_markdown(report)
    assert "1 newly failing" in markdown
    assert "1 slower" in markdown
    assert "broke" in markdown


def test_long_lists_are_truncated_rather_than_hidden_behind_a_toggle():
    # A comment that needs a click to reveal bad news is a comment that hides
    # bad news.
    report = report_for(
        {f"a::b::t{i:02d}": current("F") for i in range(25)},
        {f"a::b::t{i:02d}": history("PPPPP") for i in range(25)},
    )
    markdown = render_markdown(report, limit=10)
    assert "and 15 more" in markdown
    assert "<details>" not in markdown


def test_the_dashboard_link_is_included_when_given():
    report = report_for({"a::b::t": current("P")}, {"a::b::t": history("PPPPP")})
    markdown = render_markdown(report, dashboard_url="https://example.test/suites/admin-e2e")
    assert "[Open in TestPulse](https://example.test/suites/admin-e2e)" in markdown


def test_the_comment_records_which_run_and_commit_it_describes():
    report = report_for({"a::b::t": current("P")}, {"a::b::t": history("PPPPP")})
    markdown = render_markdown(report)
    assert "Run 999" in markdown
    assert "head123" in markdown


def test_a_skip_does_not_make_a_long_broken_test_look_newly_failing():
    # Found by reading real output: a test that had failed its last eight real
    # runs was listed under "Started failing" because the most recent entry in
    # its history happened to be a skip. "Was it failing" has to mean the last
    # time it actually ran.
    report = report_for({"a::b::t": current("F")}, {"a::b::t": history("FFFFFFFFS")})
    assert report.new_failures == []
    assert [t.display_name for t in report.still_failing] == ["t"]


def test_a_skip_does_not_hide_a_genuine_new_failure():
    # The mirror case: last real run passed, then a skip, then a failure. That
    # is still a new failure.
    report = report_for({"a::b::t": current("F")}, {"a::b::t": history("PPPPPS")})
    assert [t.display_name for t in report.new_failures] == ["t"]
