"""Write authentication.

Reads stay open; writes need a key. That split is deliberate rather than lazy:
the read side is a dashboard whose whole purpose is being linkable, and putting a
login in front of "here is the flaky test I want you to look at" defeats the
tool. The write side accepts a file, unpacks it, and stores it, and anyone who
can reach it can pollute every metric the dashboard shows.

Deliberately not OAuth, not JWT, not a user model. The client is a CI job with a
secret in its environment, and there is exactly one thing it is allowed to do.
A bearer token compared in constant time is the right size for that problem;
anything more is machinery with no corresponding requirement.
"""

from __future__ import annotations

import hmac
import os
from typing import Annotated

from fastapi import Depends, Header, HTTPException, status

INGEST_KEY_ENV = "TESTPULSE_INGEST_KEYS"
"""Comma-separated. Plural because rotating a single key means a window where
either the old CI jobs or the new ones are broken; with a list you add the new
key, migrate the jobs, then drop the old one."""


def configured_keys() -> list[str]:
    raw = os.environ.get(INGEST_KEY_ENV, "")
    return [key.strip() for key in raw.split(",") if key.strip()]


def require_ingest_key(
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    """Reject a write without a valid bearer token.

    **Open when no keys are configured**, and loudly so — the CLI and a local
    docker-compose are the common cases and neither should need a secret to try
    the thing out. The deployment guide makes setting a key a required step, and
    the API refuses to start in production mode without one (see main.py), so
    "no keys" cannot silently become the state of a public instance.

    Comparison is constant-time. A plain ``==`` on a secret leaks its length and
    prefix through timing, which is a small hole but a completely free one to
    close.
    """
    keys = configured_keys()
    if not keys:
        return

    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Ingest requires an Authorization: Bearer <key> header.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    presented = authorization[7:].strip()
    # any() over a generator would short-circuit and reintroduce the timing leak
    # this is here to avoid, so every key is compared.
    matches = [hmac.compare_digest(presented, key) for key in keys]
    if not any(matches):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid ingest key.",
        )


IngestAuth = Annotated[None, Depends(require_ingest_key)]
