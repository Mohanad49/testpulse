"""Configuration.

Every threshold that decides something lives here, never as a literal inside the
code that uses it. Flake detection is the reason: "is this test flaky" is a
policy question, not a fact, and a team should be able to change the policy
without editing Python. A number buried in a function is also a number nobody can
justify when asked.

Precedence, highest first: environment variables, then ``.env``, then
``testpulse.toml``, then the defaults below.
"""

from __future__ import annotations

from pydantic import BaseModel, Field
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)


class FlakeConfig(BaseModel):
    """Thresholds for the two flake-detection strategies.

    The defaults are chosen, not arbitrary, and the reasoning is in DECISIONS.md.
    Short version for whoever reads this file first:

    ``pass_rate_lower`` at 0.05 excludes tests that are simply broken. A test
    failing 100% of the time is not flaky, it is failing, and putting it on the
    flakiness leaderboard buries the tests that are actually non-deterministic.

    ``pass_rate_upper`` at 0.95 excludes a test with one unlucky failure in a
    long window of passes. Over 50 runs that is a single bad night, not a
    pattern.

    ``flip_rate_threshold`` at 0.20 is what separates "intermittent" from
    "regressed". A test that passed 30 times then failed 20 times has a pass rate
    of 0.6 and sits squarely inside the band above, but it flipped exactly once.
    Without the flip gate it would be reported as the flakiest test in the suite
    when it is actually a clean regression with a specific cause.
    """

    window_size: int = Field(default=50, ge=2)
    """How many recent runs of a suite to consider. Larger means more confident
    and slower to react to a fix; smaller means twitchy."""

    pass_rate_lower: float = Field(default=0.05, ge=0.0, le=1.0)
    pass_rate_upper: float = Field(default=0.95, ge=0.0, le=1.0)
    flip_rate_threshold: float = Field(default=0.20, ge=0.0, le=1.0)

    min_scored_runs: int = Field(default=5, ge=2)
    """How much history the rolling-flip strategy needs before it will call
    anything flaky.

    Without this it fires on two runs. One pass and one fail gives a pass rate of
    0.5 and a flip rate of 1.0, which clears every gate above, and a test that
    has run twice and failed once is far more likely to be a regression somebody
    should look at than a flaky test to quarantine. Five is a judgement: enough
    that a single alternation cannot carry it, few enough that a new test does
    not stay invisible for weeks. Strategy A is not gated by this, because its
    evidence does not get stronger with repetition - one same-commit
    disagreement already means the code did not change and the outcome did."""

    strategies: tuple[str, ...] = ("same-commit", "rolling-flip")
    """Which classifiers to run. Both by default. ``same-commit`` is high
    precision and low recall (it needs retries or repeated runs to see anything);
    ``rolling-flip`` is the opposite. Running only one is a legitimate choice and
    the reason this is configurable rather than fixed."""


class NewlyFailingConfig(BaseModel):
    """When to call a test newly failing rather than flaky.

    These two are easy to confuse and need opposite responses: a newly failing
    test usually has a specific cause someone can find, a flaky test usually
    does not.
    """

    min_consecutive_failures: int = Field(default=2, ge=1)
    """One failure is noise. Two in a row is a pattern worth naming."""

    min_prior_runs: int = Field(default=3, ge=1)
    """How much passing history is needed before "it used to pass" means
    anything. Without this a test's second ever run failing looks like a
    regression."""

    prior_pass_rate: float = Field(default=1.0, ge=0.0, le=1.0)
    """How clean the history before the failures has to be. Defaulting to 1.0
    (spotless) keeps this signal separate from flakiness: if the earlier runs
    were already mixed, that is a flaky test, not a new failure."""


class QuarantineConfig(BaseModel):
    default_expires_after_days: int = Field(default=14, ge=1)
    """Every quarantine entry gets an expiry so the list cannot quietly turn into
    a graveyard of tests nobody runs and nobody remembers disabling."""


class Settings(BaseSettings):
    """Runtime settings.

    Nested values use a double underscore in the environment, so the flake window
    is ``TESTPULSE_FLAKE__WINDOW_SIZE``.
    """

    model_config = SettingsConfigDict(
        env_prefix="TESTPULSE_",
        env_nested_delimiter="__",
        env_file=".env",
        toml_file="testpulse.toml",
        extra="ignore",
    )

    database_url: str = "sqlite:///testpulse.db"
    flake: FlakeConfig = FlakeConfig()
    newly_failing: NewlyFailingConfig = NewlyFailingConfig()
    quarantine: QuarantineConfig = QuarantineConfig()

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Put the TOML file below the environment.

        CI needs to override a threshold for one job without editing a committed
        file, and a committed file needs to be the team's shared default. That
        only works in this order.
        """
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            TomlConfigSettingsSource(settings_cls),
            file_secret_settings,
        )


def get_settings() -> Settings:
    """Return settings. Separate from the class so tests can monkeypatch it."""
    return Settings()
