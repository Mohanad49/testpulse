"""Engine and session construction."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session, sessionmaker

from testpulse_core.config import get_settings


def create_db_engine(database_url: str | None = None) -> Engine:
    """Build an engine for SQLite or Postgres.

    SQLite needs foreign key enforcement switched on per connection — it is off
    by default, which would let the ON DELETE CASCADE on test_results silently
    do nothing locally while working correctly on Postgres. A constraint that
    only holds in production is worse than no constraint, because local tests
    pass either way.
    """
    url = database_url or get_settings().database_url
    engine = create_engine(url, future=True)

    if url.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def _enable_foreign_keys(dbapi_connection: object, _record: object) -> None:
            cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    return engine


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


@contextmanager
def session_scope(engine: Engine) -> Iterator[Session]:
    """Session with commit-on-success, rollback-on-error.

    An ingest is all-or-nothing. A partially written run would look to Phase 2
    like a suite that stopped mid-execution, which is a real failure mode it is
    supposed to detect — so writing one accidentally would manufacture a false
    signal in the product's own data.
    """
    session = create_session_factory(engine)()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def connection_summary(connection: Connection) -> str:
    """Human-readable target for CLI output, without leaking credentials."""
    url = connection.engine.url
    return f"{url.drivername}:{url.database}"
