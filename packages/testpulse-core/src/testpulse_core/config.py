"""Configuration.

Every tunable lives here or in a config file, never as a literal inside logic.
Phase 2 introduces flake thresholds where this matters most: a threshold buried
in a function is a threshold nobody can justify or change per project, and
"why 0.2?" is a question that needs an answer a reader can find.
"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings, read from the environment or a ``.env`` file.

    ``TESTPULSE_`` prefixed environment variables win over ``.env``, which wins
    over these defaults — the usual precedence, so CI can override without a
    file present.
    """

    model_config = SettingsConfigDict(
        env_prefix="TESTPULSE_",
        env_file=".env",
        extra="ignore",
    )

    database_url: str = "sqlite:///testpulse.db"
    """SQLite for local use, Postgres for a deployed instance. The schema is
    written to the SQLAlchemy Core subset both support, so this is genuinely a
    one-variable switch rather than an aspiration."""


def get_settings() -> Settings:
    """Return settings. Separate from the class so tests can monkeypatch it."""
    return Settings()
