"""Per-test health metrics.

Everything here is a pure function over a sequence of observations, ordered
oldest to newest. No database, no config lookup at module level. That keeps the
definitions testable in isolation, which matters more here than anywhere else in
the project: these numbers are the product, and a subtly wrong percentile or an
off-by-one in a flip count is invisible in a dashboard.

A note on skipped results, because it affects almost every function below.
Skipped observations are filtered out before any metric is computed. A skipped
test did not pass and did not fail; counting it either way is a lie. Counting it
as a denominator would mean a test skipped for 40 of 50 runs shows a pass rate of
0.2 while never having failed once.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from itertools import pairwise

from testpulse_core.config import FlakeConfig, NewlyFailingConfig
from testpulse_core.models import TestStatus

FAILING_STATUSES = frozenset({TestStatus.FAILED, TestStatus.ERROR})


@dataclass(frozen=True, slots=True)
class Observation:
    """One test's outcome in one run, with the context needed to judge it."""

    run_id: int
    started_at: datetime
    status: TestStatus
    duration_ms: int
    commit_sha: str | None = None
    retry_count: int | None = None

    @property
    def is_pass(self) -> bool:
        return self.status is TestStatus.PASSED

    @property
    def is_failure(self) -> bool:
        return self.status in FAILING_STATUSES


@dataclass(frozen=True, slots=True)
class TestMetrics:
    """Computed health for one test over one window."""

    __test__ = False

    test_id: str
    display_name: str
    runs_in_window: int
    scored_runs: int
    """Runs that counted, i.e. excluding skips. Reported because a metric drawn
    from 3 of 50 runs deserves less trust than one drawn from 50, and the
    dashboard should be able to say so instead of showing both as equal."""

    pass_rate: float | None
    flip_rate: float
    flakiness_score: float
    mean_duration_ms: float
    p95_duration_ms: int
    duration_trend_ms_per_run: float
    first_seen_at: datetime
    last_failed_at: datetime | None
    consecutive_failures: int
    is_newly_failing: bool
    is_flaky: bool
    flake_evidence: tuple[str, ...]
    """Which strategies fired. Empty when not flaky. Kept because "this test is
    flaky" is not actionable on its own, and the two strategies mean different
    things: same-commit evidence is close to proof, rolling-flip is an inference
    from a pattern."""


def scored(observations: list[Observation]) -> list[Observation]:
    """Drop skips. Every metric below operates on the result."""
    return [o for o in observations if o.status is not TestStatus.SKIPPED]


def pass_rate(observations: list[Observation]) -> float | None:
    """Fraction of non-skipped runs that passed, or None if nothing counted.

    None rather than 0.0 for an all-skipped test. Zero would put it at the bottom
    of a "worst pass rate" list next to tests that genuinely fail every time.
    """
    counted = scored(observations)
    if not counted:
        return None
    return sum(1 for o in counted if o.is_pass) / len(counted)


def flip_rate(observations: list[Observation]) -> float:
    """How often the outcome changed between consecutive runs, normalised to 0-1.

    Comparison is on pass/not-pass, not on the four-value status. A test going
    from failed to error has not flipped in any sense a human cares about; it
    failed both times, for a slightly different reason. Counting that as a flip
    would rank tests with unstable *failure modes* above tests that are actually
    non-deterministic.

    Denominator is the number of adjacent pairs, so a window of n scored runs has
    n-1 chances to flip. Fewer than two runs means no chance to flip, which is
    0.0 and not undefined: no evidence of flipping is not the same as missing
    data here, because the question "did it change" has a definite answer.
    """
    counted = scored(observations)
    if len(counted) < 2:
        return 0.0
    flips = sum(1 for a, b in pairwise(counted) if a.is_pass != b.is_pass)
    return flips / (len(counted) - 1)


def flakiness_score(rate: float | None, flips: float) -> float:
    """Single 0-1 ranking number combining pass rate and flip rate.

    ``flip_rate * 4 * p * (1 - p)``.

    The second factor is a parabola peaking at 1.0 when the pass rate is 0.5 and
    falling to 0 at either extreme. That encodes the thing that makes flakiness
    different from failure: a test that always fails is perfectly predictable, so
    it scores 0 no matter what, and a test that passes half the time and flips
    constantly scores near 1. Multiplying by flip rate then separates "coin toss
    every run" from "passed 25 times then failed 25 times", which have the same
    pass rate and are completely different problems.

    This is a ranking aid, not a probability. It exists so the leaderboard has a
    defensible sort order; the classifier decides flaky or not.
    """
    if rate is None:
        return 0.0
    return flips * 4.0 * rate * (1.0 - rate)


def mean_duration_ms(observations: list[Observation]) -> float:
    counted = scored(observations)
    if not counted:
        return 0.0
    return sum(o.duration_ms for o in counted) / len(counted)


def p95_duration_ms(observations: list[Observation]) -> int:
    """95th percentile duration by nearest-rank.

    Nearest-rank rather than interpolation because the result is always a
    duration that actually happened. On the small windows this works with (50
    runs means the p95 is the 48th value) interpolating between two samples
    invents a number no run ever produced, and "this test took 4.2s at p95" is
    easier to defend when some run really did take 4.2s.
    """
    counted = scored(observations)
    if not counted:
        return 0
    durations = sorted(o.duration_ms for o in counted)
    rank = math.ceil(0.95 * len(durations))
    return durations[max(rank - 1, 0)]


def duration_trend_ms_per_run(observations: list[Observation]) -> float:
    """Least-squares slope of duration against run index, in ms per run.

    Positive means getting slower. Deliberately reported per run rather than as a
    percentage, so a 400ms drift reads the same whether the test takes 1s or 30s;
    the dashboard can turn it into a percentage where that is more useful.

    Sensitive to outliers, since it is ordinary least squares. One 30-second
    timeout in a window of fast runs will tilt the line. Acceptable for a trend
    arrow, not something to alert on.
    """
    counted = scored(observations)
    n = len(counted)
    if n < 2:
        return 0.0
    mean_x = (n - 1) / 2
    mean_y = sum(o.duration_ms for o in counted) / n
    numerator = sum((i - mean_x) * (o.duration_ms - mean_y) for i, o in enumerate(counted))
    denominator = sum((i - mean_x) ** 2 for i in range(n))
    if denominator == 0:
        return 0.0
    return numerator / denominator


def consecutive_failures(observations: list[Observation]) -> int:
    """Length of the current unbroken run of failures, counting back from newest."""
    counted = scored(observations)
    streak = 0
    for observation in reversed(counted):
        if not observation.is_failure:
            break
        streak += 1
    return streak


def last_failed_at(observations: list[Observation]) -> datetime | None:
    for observation in reversed(observations):
        if observation.is_failure:
            return observation.started_at
    return None


def is_newly_failing(observations: list[Observation], config: NewlyFailingConfig) -> bool:
    """True when a test that used to pass has started failing and stayed failing.

    Three conditions, all required:

    1. The most recent runs are an unbroken failure streak of at least
       ``min_consecutive_failures``. One failure is noise.
    2. There are at least ``min_prior_runs`` runs before that streak. Without
       this, a test whose second ever run fails looks like a regression.
    3. Those earlier runs passed at a rate of at least ``prior_pass_rate``,
       which defaults to spotless.

    Condition 3 is what keeps this from overlapping with flakiness. If the
    history before the streak was already mixed, the test was flaky and has now
    tipped over, which is a different conversation than "this broke on Tuesday".
    """
    counted = scored(observations)
    streak = consecutive_failures(counted)
    if streak < config.min_consecutive_failures:
        return False
    prior = counted[: len(counted) - streak]
    if len(prior) < config.min_prior_runs:
        return False
    prior_passes = sum(1 for o in prior if o.is_pass) / len(prior)
    return prior_passes >= config.prior_pass_rate


def same_commit_disagreement(observations: list[Observation]) -> bool:
    """Strategy A. High precision, low recall.

    A test is flaky if it produced different outcomes without the code changing.
    Two independent sources of that evidence:

    **Across runs.** Two or more runs against the same ``commit_sha`` where the
    test passed in one and did not in another. Nothing about the code differed,
    so the difference came from somewhere else.

    **Within a run.** A result with ``retry_count > 0`` that ended up passing.
    Runners only retry a test that did not pass, so a passing result with retries
    means the same binary, on the same commit, produced both outcomes minutes
    apart. This is why ``retry_count`` is nullable rather than defaulting to
    zero: a format that cannot report retries must not look like a format
    reporting none.

    Nearly no false positives, which is the point. It also finds nothing at all
    unless the suite retries or CI runs the same commit twice, which is why it is
    not the only strategy.
    """
    for observation in observations:
        if (
            observation.retry_count is not None
            and observation.retry_count > 0
            and observation.is_pass
        ):
            return True

    by_commit: dict[str, set[bool]] = defaultdict(set)
    for observation in scored(observations):
        if observation.commit_sha:
            by_commit[observation.commit_sha].add(observation.is_pass)
    return any(len(outcomes) > 1 for outcomes in by_commit.values())


def rolling_flip_flaky(
    rate: float | None,
    flips: float,
    scored_runs: int,
    config: FlakeConfig,
) -> bool:
    """Strategy B. High recall, lower precision.

    Flaky if there is enough history to judge, the pass rate sits strictly
    between the bounds, *and* the flip rate clears the threshold. The three gates
    exclude different things: the run count removes tests with too little
    evidence, the bounds remove tests that are consistently broken or
    consistently fine, and the flip gate removes clean regressions that happen to
    land in the middle of the band.

    The run-count gate was added after a two-run history of one pass and one fail
    was classified flaky. That input gives a pass rate of 0.5 and a flip rate of
    1.0, so it clears both thresholds, and it is much more likely to be a
    regression worth investigating than a flaky test worth quarantining.

    Works on any suite with history, needs no retries and no repeated commits.
    The cost is that a test affected by a real intermittent bug in the product is
    indistinguishable from a badly written test, because from here they look
    identical. That is a limit of the approach rather than a bug in it, and it is
    the main argument for running Strategy A alongside.
    """
    if rate is None or scored_runs < config.min_scored_runs:
        return False
    return config.pass_rate_lower < rate < config.pass_rate_upper and (
        flips > config.flip_rate_threshold
    )


def classify(
    observations: list[Observation],
    config: FlakeConfig,
) -> tuple[bool, tuple[str, ...]]:
    """Run the configured strategies and report which ones fired."""
    rate = pass_rate(observations)
    flips = flip_rate(observations)
    evidence: list[str] = []

    if "same-commit" in config.strategies and same_commit_disagreement(observations):
        evidence.append("same-commit")
    if "rolling-flip" in config.strategies and rolling_flip_flaky(
        rate, flips, len(scored(observations)), config
    ):
        evidence.append("rolling-flip")

    return bool(evidence), tuple(evidence)


def compute(
    test_id: str,
    display_name: str,
    observations: list[Observation],
    first_seen: datetime,
    flake_config: FlakeConfig,
    newly_failing_config: NewlyFailingConfig,
) -> TestMetrics:
    """Build the full metric set for one test from its window of observations."""
    ordered = sorted(observations, key=lambda o: o.started_at)
    rate = pass_rate(ordered)
    flips = flip_rate(ordered)
    flaky, evidence = classify(ordered, flake_config)

    return TestMetrics(
        test_id=test_id,
        display_name=display_name,
        runs_in_window=len(ordered),
        scored_runs=len(scored(ordered)),
        pass_rate=rate,
        flip_rate=flips,
        flakiness_score=flakiness_score(rate, flips),
        mean_duration_ms=mean_duration_ms(ordered),
        p95_duration_ms=p95_duration_ms(ordered),
        duration_trend_ms_per_run=duration_trend_ms_per_run(ordered),
        first_seen_at=first_seen,
        last_failed_at=last_failed_at(ordered),
        consecutive_failures=consecutive_failures(ordered),
        is_newly_failing=is_newly_failing(ordered, newly_failing_config),
        is_flaky=flaky,
        flake_evidence=evidence,
    )
