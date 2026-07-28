"""Shared dependencies.

The engine is built once and cached rather than per request. Creating an engine
per request throws away the connection pool, which on Postgres means a fresh TCP
connection and authentication handshake for every call.
"""

from __future__ import annotations

from collections.abc import Iterator
from functools import lru_cache
from typing import Annotated

from fastapi import Depends
from sqlalchemy import Engine
from sqlalchemy.orm import Session
from testpulse_core.config import Settings, get_settings
from testpulse_core.storage.db import create_db_engine, create_session_factory


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    return create_db_engine(get_settings().database_url)


@lru_cache(maxsize=1)
def get_config() -> Settings:
    """Settings are read once per process.

    Flake thresholds change the meaning of every number the API returns, so
    re-reading them mid-process would let two requests in the same second answer
    the same question differently. Restarting to pick up a config change is the
    honest behaviour.
    """
    return get_settings()


def get_session() -> Iterator[Session]:
    """A read-scoped session per request.

    No commit here. Every read endpoint is read-only, and the one write endpoint
    manages its own transaction, so a blanket commit-on-exit would only create
    opportunities to write something nobody asked for.
    """
    session = create_session_factory(get_engine())()
    try:
        yield session
    finally:
        session.close()


SessionDep = Annotated[Session, Depends(get_session)]
ConfigDep = Annotated[Settings, Depends(get_config)]
EngineDep = Annotated[Engine, Depends(get_engine)]
"""For the write path, which needs its own transaction rather than the
read-scoped session. It has to arrive by injection like everything else: calling
get_engine() directly inside a handler bypasses dependency overrides, which means
writes and reads can end up pointed at two different databases without anything
complaining."""
