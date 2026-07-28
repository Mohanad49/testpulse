"""Metrics engine tests.

Two halves. Example-based tests pin the specific behaviours the definitions were
written for, in cases small enough to check by hand. Property tests at the bottom
cover the invariants that must hold for every possible history, which is where
the real risk lives: it is easy to write a percentile or a flip count that is
right on the sequences you thought of.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from hypothesis import given, settings
from hypothesis import strategies as st

from testpulse_core.config import FlakeConfig, NewlyFailingConfig
from testpulse_core.metrics import (
    Observation,
    classify,
    consecutive_failures,
    duration_trend_ms_per_run,
    flakiness_score,
    flip_rate,
    is_newly_failing,
    last_failed_at,
    mean_duration_ms,
    p95_duration_ms,
    pass_rate,
    rolling_flip_flaky,
    same_commit_disagreement,
    scored,
)
from testpulse_core.models import TestStatus

START = datetime(2026, 7, 1, tzinfo=UTC)
FLAKE = FlakeConfig()
NEWLY = NewlyFailingConfig()

STATUS_CHARS = {
    "P": TestStatus.PASSED,
    "F": TestStatus.FAILED,
    "E": TestStatus.ERROR,
    "S": TestStatus.SKIPPED,
}


def history(
    pattern: str,
    *,
    durations: list[int] | None = None,
    commits: list[str] | None = None,
    retries: list[int | None] | None = None,
) -> list[Observation]:
    """Build a history from a compact string, oldest first.

    "PPFPP" reads as passed, passed, failed, passed, passed. Keeps the test
    bodies readable, which matters when the thing under test is a definition
    rather than a behaviour.
    """
    observations = []
    for index, char in enumerate(pattern):
        observations.append(
            Observation(
                run_id=index + 1,
                started_at=START + timedelta(hours=index),
                status=STATUS_CHARS[char],
                duration_ms=durations[index] if durations else 100,
                commit_sha=commits[index] if commits else f"sha{index}",
                retry_count=retries[index] if retries else None,
            )
        )
    return observations


# --------------------------------------------------------------------------
# pass rate and skips
# --------------------------------------------------------------------------


def test_pass_rate_is_the_obvious_fraction():
    assert pass_rate(history("PPPF")) == 0.75


def test_skips_are_excluded_from_the_denominator():
    # A test skipped for most of the window has not failed. Counting skips would
    # report a pass rate of 0.25 for something that passed every time it ran.
    assert pass_rate(history("PSSS")) == 1.0
    assert len(scored(history("PSSS"))) == 1


def test_all_skipped_gives_none_not_zero():
    # Zero would sort it next to tests that genuinely fail every run.
    assert pass_rate(history("SSS")) is None


def test_errors_count_as_not_passing():
    assert pass_rate(history("PPEE")) == 0.5


# --------------------------------------------------------------------------
# flip rate
# --------------------------------------------------------------------------


def test_flip_rate_counts_transitions_not_failures():
    # Four scored runs, three adjacent pairs, one transition.
    assert flip_rate(history("PPFF")) == 1 / 3


def test_alternating_history_flips_every_time():
    assert flip_rate(history("PFPFP")) == 1.0


def test_stable_history_never_flips():
    assert flip_rate(history("PPPPP")) == 0.0
    assert flip_rate(history("FFFFF")) == 0.0


def test_failed_to_error_is_not_a_flip():
    # It failed both times. Counting this would rank tests with unstable failure
    # modes above tests that are actually non-deterministic.
    assert flip_rate(history("FEFE")) == 0.0


def test_single_run_cannot_flip():
    assert flip_rate(history("P")) == 0.0


# --------------------------------------------------------------------------
# flakiness score
# --------------------------------------------------------------------------


def test_always_failing_test_scores_zero():
    # Perfectly predictable. It is broken, not flaky, and it does not belong on
    # the flakiness leaderboard.
    assert flakiness_score(pass_rate(history("FFFF")), flip_rate(history("FFFF"))) == 0.0


def test_coin_toss_scores_near_one():
    obs = history("PFPFPFPF")
    assert flakiness_score(pass_rate(obs), flip_rate(obs)) > 0.9


def test_one_clean_regression_scores_below_a_coin_toss():
    # Same pass rate (0.5), completely different problem: this flipped once.
    regression = history("PPPPFFFF")
    tossup = history("PFPFPFPF")
    assert pass_rate(regression) == pass_rate(tossup) == 0.5
    assert flakiness_score(pass_rate(regression), flip_rate(regression)) < flakiness_score(
        pass_rate(tossup), flip_rate(tossup)
    )


# --------------------------------------------------------------------------
# durations
# --------------------------------------------------------------------------


def test_p95_returns_a_duration_that_actually_happened():
    durations = [100, 200, 300, 400, 5000]
    obs = history("PPPPP", durations=durations)
    assert p95_duration_ms(obs) in durations
    assert p95_duration_ms(obs) == 5000


def test_mean_ignores_skipped_runs():
    obs = history("PSP", durations=[100, 99999, 300])
    assert mean_duration_ms(obs) == 200


def test_duration_trend_is_positive_when_getting_slower():
    obs = history("PPPPP", durations=[100, 200, 300, 400, 500])
    assert duration_trend_ms_per_run(obs) == 100.0


def test_duration_trend_is_negative_when_getting_faster():
    obs = history("PPPP", durations=[400, 300, 200, 100])
    assert duration_trend_ms_per_run(obs) == -100.0


def test_flat_durations_have_no_trend():
    assert duration_trend_ms_per_run(history("PPPP", durations=[250] * 4)) == 0.0


# --------------------------------------------------------------------------
# failure streaks and newly-failing
# --------------------------------------------------------------------------


def test_consecutive_failures_counts_back_from_the_newest():
    assert consecutive_failures(history("PPFFF")) == 3
    assert consecutive_failures(history("FFFPP")) == 0


def test_last_failed_at_finds_the_most_recent_failure():
    obs = history("PFPPP")
    assert last_failed_at(obs) == START + timedelta(hours=1)


def test_last_failed_at_is_none_for_a_clean_test():
    assert last_failed_at(history("PPPP")) is None


def test_clean_history_then_a_failure_streak_is_newly_failing():
    assert is_newly_failing(history("PPPPPFF"), NEWLY) is True


def test_one_failure_is_not_yet_newly_failing():
    # One failure is noise. The default asks for two in a row.
    assert is_newly_failing(history("PPPPPF"), NEWLY) is False


def test_a_test_with_no_history_is_not_newly_failing():
    # Its second ever run failing is not a regression, there is nothing to
    # regress from.
    assert is_newly_failing(history("PFF"), NEWLY) is False


def test_a_messy_history_then_failures_is_not_newly_failing():
    # This was already flaky and has tipped over. Different conversation than
    # "this broke on Tuesday", so it must not claim to be a new failure.
    assert is_newly_failing(history("PFPFPFF"), NEWLY) is False


def test_newly_failing_and_flaky_are_mutually_exclusive_on_these_examples():
    clean_regression = history("PPPPPPPPFF")
    assert is_newly_failing(clean_regression, NEWLY) is True
    assert classify(clean_regression, FLAKE)[0] is False


# --------------------------------------------------------------------------
# Strategy A: same-commit disagreement
# --------------------------------------------------------------------------


def test_same_commit_with_different_outcomes_is_flaky():
    # The code did not change between these two runs.
    obs = history("PF", commits=["abc", "abc"])
    assert same_commit_disagreement(obs) is True


def test_same_commit_agreeing_is_not_evidence():
    assert same_commit_disagreement(history("FF", commits=["abc", "abc"])) is False


def test_different_commits_disagreeing_is_not_evidence():
    # Somebody changed the code. That is what a failing test is supposed to do.
    assert same_commit_disagreement(history("PF", commits=["abc", "def"])) is False


def test_a_retried_test_that_went_green_is_evidence_on_its_own():
    # Runners only retry a test that did not pass, so a passing result with
    # retries means one binary produced both outcomes minutes apart.
    obs = history("P", retries=[1])
    assert same_commit_disagreement(obs) is True


def test_a_test_that_failed_every_retry_is_not_evidence():
    assert same_commit_disagreement(history("F", retries=[2])) is False


def test_null_retry_count_is_not_treated_as_zero_or_as_evidence():
    assert same_commit_disagreement(history("P", retries=[None])) is False


def test_runs_without_a_commit_sha_are_ignored_by_strategy_a():
    obs = [
        Observation(1, START, TestStatus.PASSED, 10, commit_sha=None),
        Observation(2, START + timedelta(hours=1), TestStatus.FAILED, 10, commit_sha=None),
    ]
    assert same_commit_disagreement(obs) is False


# --------------------------------------------------------------------------
# Strategy B: rolling flip rate
# --------------------------------------------------------------------------


def test_alternating_test_is_caught_by_rolling_flip():
    obs = history("PFPFPFPF")
    assert rolling_flip_flaky(pass_rate(obs), flip_rate(obs), len(scored(obs)), FLAKE) is True


def test_consistently_broken_test_is_excluded_by_the_lower_bound():
    obs = history("FFFFFFFF")
    assert rolling_flip_flaky(pass_rate(obs), flip_rate(obs), len(scored(obs)), FLAKE) is False


def test_one_unlucky_failure_in_a_long_window_is_excluded_by_the_upper_bound():
    obs = history("P" * 49 + "F")
    assert pass_rate(obs) == 0.98
    assert rolling_flip_flaky(pass_rate(obs), flip_rate(obs), len(scored(obs)), FLAKE) is False


def test_a_clean_regression_is_excluded_by_the_flip_gate():
    # This is the case the flip threshold exists for. Pass rate 0.6 sits inside
    # the band, but it flipped exactly once out of 49 chances.
    obs = history("P" * 30 + "F" * 20)
    assert FLAKE.pass_rate_lower < (pass_rate(obs) or 0) < FLAKE.pass_rate_upper
    assert rolling_flip_flaky(pass_rate(obs), flip_rate(obs), len(scored(obs)), FLAKE) is False


def test_thresholds_come_from_config_not_from_the_code():
    obs = history("P" * 30 + "F" * 20)
    permissive = FlakeConfig(flip_rate_threshold=0.0)
    assert rolling_flip_flaky(pass_rate(obs), flip_rate(obs), len(scored(obs)), permissive) is True


# --------------------------------------------------------------------------
# strategy selection
# --------------------------------------------------------------------------


def test_evidence_names_the_strategy_that_fired():
    obs = history("PF", commits=["abc", "abc"])
    flaky, evidence = classify(obs, FlakeConfig(strategies=("same-commit",)))
    assert flaky is True
    assert evidence == ("same-commit",)


def test_both_strategies_can_fire_together():
    obs = history("PFPFPFPF", commits=["abc"] * 8)
    flaky, evidence = classify(obs, FLAKE)
    assert flaky is True
    assert set(evidence) == {"same-commit", "rolling-flip"}


def test_disabling_a_strategy_disables_its_findings():
    obs = history("PFPFPFPF", commits=["abc"] * 8)
    flaky, evidence = classify(obs, FlakeConfig(strategies=("rolling-flip",)))
    assert flaky is True
    assert evidence == ("rolling-flip",)

    flaky, evidence = classify(obs, FlakeConfig(strategies=()))
    assert flaky is False
    assert evidence == ()


def test_two_runs_are_not_enough_evidence_for_rolling_flip():
    # One pass and one fail gives pass rate 0.5 and flip rate 1.0, which clears
    # every threshold. Far more likely a regression than a flaky test, so the
    # run-count floor stops it. Strategy A is not gated the same way.
    obs = history("PF", commits=["abc", "def"])
    assert classify(obs, FlakeConfig(strategies=("rolling-flip",))) == (False, ())

    same_commit = history("PF", commits=["abc", "abc"])
    assert classify(same_commit, FLAKE)[1] == ("same-commit",)


# --------------------------------------------------------------------------
# Properties. These must hold for any history, not just the ones above.
# --------------------------------------------------------------------------

any_history = st.lists(
    st.sampled_from("PFES"),
    min_size=1,
    max_size=60,
).map(lambda chars: "".join(chars))


@given(pattern=st.lists(st.just("P"), min_size=1, max_size=60).map("".join))
def test_a_test_that_always_passes_is_never_flaky(pattern):
    # The single most important property in the project. If this ever fails,
    # every number the dashboard shows is suspect.
    obs = history(pattern)
    assert classify(obs, FLAKE) == (False, ())


@given(pattern=st.lists(st.just("P"), min_size=1, max_size=40).map("".join))
def test_an_always_passing_test_is_never_flaky_under_repeated_commits(pattern):
    # Same commit ingested many times, always green. Strategy A must not read
    # repetition alone as disagreement.
    obs = history(pattern, commits=["same-sha"] * len(pattern))
    assert same_commit_disagreement(obs) is False


@given(pattern=any_history)
@settings(max_examples=300)
def test_rates_stay_within_bounds(pattern):
    obs = history(pattern)
    rate = pass_rate(obs)
    flips = flip_rate(obs)
    assert rate is None or 0.0 <= rate <= 1.0
    assert 0.0 <= flips <= 1.0
    assert 0.0 <= flakiness_score(rate, flips) <= 1.0


@given(pattern=any_history)
@settings(max_examples=300)
def test_a_test_that_never_fails_has_no_failure_signals(pattern):
    clean = pattern.replace("F", "P").replace("E", "P")
    obs = history(clean)
    assert consecutive_failures(obs) == 0
    assert last_failed_at(obs) is None
    assert is_newly_failing(obs, NEWLY) is False


@given(pattern=any_history)
@settings(max_examples=300)
def test_p95_is_always_a_real_observed_duration(pattern):
    durations = [(i + 1) * 37 for i in range(len(pattern))]
    obs = history(pattern, durations=durations)
    counted = scored(obs)
    if counted:
        assert p95_duration_ms(obs) in {o.duration_ms for o in counted}
    else:
        assert p95_duration_ms(obs) == 0


@given(pattern=any_history)
@settings(max_examples=300)
def test_consecutive_failures_never_exceeds_the_scored_window(pattern):
    obs = history(pattern)
    assert 0 <= consecutive_failures(obs) <= len(scored(obs))


@given(pattern=any_history)
@settings(max_examples=300)
def test_newly_failing_implies_a_current_failure_streak(pattern):
    obs = history(pattern)
    if is_newly_failing(obs, NEWLY):
        assert consecutive_failures(obs) >= NEWLY.min_consecutive_failures
        assert last_failed_at(obs) is not None
