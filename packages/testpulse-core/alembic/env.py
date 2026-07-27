"""Alembic environment.

The database URL comes from TestPulse's own settings rather than from
alembic.ini, so migrations and the application can never point at different
databases — a mistake that stays invisible until a migration "succeeds" against
the wrong target.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context

from testpulse_core.config import get_settings
from testpulse_core.storage.db import create_db_engine
from testpulse_core.storage.schema import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _database_url() -> str:
    """Prefer an explicit ``-x url=...`` override, then settings."""
    override = context.get_x_argument(as_dictionary=True).get("url")
    return override or get_settings().database_url


def run_migrations_offline() -> None:
    """Emit SQL to stdout without connecting, for review or manual application."""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        # SQLite cannot ALTER most things in place; batch mode rewrites the table
        # instead. Enabled everywhere so a migration authored against Postgres
        # does not fail only on a contributor's local SQLite.
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = create_db_engine(_database_url())
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
        )
        with context.begin_transaction():
            context.run_migrations()
    connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
