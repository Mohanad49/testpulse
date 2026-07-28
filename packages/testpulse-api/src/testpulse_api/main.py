"""Application factory."""

from __future__ import annotations

from fastapi import FastAPI

from testpulse_api.routers import ingest, suites

DESCRIPTION = """
Read access to stored test runs, computed health metrics and flake
classifications, plus an upload endpoint for ingesting reports.

Two things worth knowing before consuming this API:

* `flakiness_score` ranks the rolling-flip strategy only. A test caught by
  same-commit evidence can legitimately score `0.0`, so never sort on the score
  alone or the most conclusively flaky tests end up at the bottom.
* `pass_rate` is `null`, not `0.0`, when nothing in the window was scored. A test
  that only ever skipped has not passed and has not failed.
"""


def create_app() -> FastAPI:
    app = FastAPI(
        title="TestPulse API",
        version="0.1.0",
        description=DESCRIPTION,
        # Served rather than disabled. The docs are the fastest way for anyone
        # reviewing this project to see what it does without cloning it.
        docs_url="/docs",
        openapi_url="/openapi.json",
    )
    app.include_router(suites.router)
    app.include_router(ingest.router)

    @app.get("/health", tags=["meta"])
    def liveness() -> dict[str, str]:
        """Liveness only. Deliberately does not touch the database.

        A health check that queries the database conflates "the process is alive"
        with "the database is reachable", and an orchestrator will then restart a
        perfectly healthy API because a database failover took four seconds.
        """
        return {"status": "ok"}

    return app


app = create_app()
