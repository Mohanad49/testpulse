# syntax=docker/dockerfile:1

# Multi-stage so the runtime image carries no build tooling. uv is only needed to
# resolve and install; it does not belong in the shipped image.
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS builder

ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
WORKDIR /app

# Dependency manifests first, source second. Editing a source file then rebuilds
# only the last layer instead of re-resolving every dependency.
COPY pyproject.toml uv.lock ./
COPY packages/testpulse-core/pyproject.toml packages/testpulse-core/
COPY packages/testpulse-api/pyproject.toml packages/testpulse-api/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev --extra postgres

COPY packages/ packages/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --extra postgres


FROM python:3.12-slim-bookworm AS runtime

# Non-root. The API accepts file uploads and unpacks archives, which is the one
# part of this system where being wrong about a path matters.
RUN useradd --create-home --uid 10001 testpulse
WORKDIR /app

COPY --from=builder --chown=testpulse:testpulse /app/.venv /app/.venv
COPY --from=builder --chown=testpulse:testpulse /app/packages /app/packages
ENV PATH="/app/.venv/bin:$PATH"

USER testpulse
EXPOSE 8000

# Hits the liveness endpoint, which deliberately does not touch the database -
# the container being up and the database being reachable are different
# questions and conflating them causes restart loops during a failover.
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2).status==200 else 1)"

CMD ["uvicorn", "testpulse_api.main:app", "--host", "0.0.0.0", "--port", "8000"]
