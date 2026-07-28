# TestPulse

Test observability and flake detection for CI test suites.

> **Status: Phase 5 of 6.** Ingestion, storage, metrics, flake detection, a REST
> API, the dashboard, and CI integration. Deployment is next. This README is a
> placeholder; the full case study is written in Phase 6.

## The problem

A regression suite in CI has no memory across runs. A test that fails
intermittently looks identical to one that just started failing. Nobody knows
which tests are slowest, which are flakiest, or whether the suite is getting
slower. TestPulse ingests results over time so those questions have answers.

## What works today

Four report formats parse into one schema and persist to SQLite or Postgres:

| Format | Input | Reports retries |
|---|---|---|
| JUnit XML | file | no |
| Playwright JSON reporter | file | **yes** |
| pytest-json-report | file | no |
| Allure results | directory | not yet |

```bash
uv sync
cd packages/testpulse-core && uv run --project .. alembic upgrade head

uv run testpulse ingest \
  --format junit --path ./reports/junit.xml \
  --suite admin-portal-e2e \
  --commit "$GITHUB_SHA" --branch "$GITHUB_REF_NAME" --env chrome-ci
```

`testpulse formats` lists the parsers this build has. `testpulse info` prints the
database it would write to.

Then ask it what it found:

```bash
testpulse metrics --suite admin-portal-e2e        # per-test health, flakiest first
testpulse flaky   --suite admin-portal-e2e        # just the flaky ones, with evidence
testpulse flaky   --suite admin-portal-e2e --fail-on-flaky   # exits 5, for gating CI

testpulse quarantine add  --suite admin-portal-e2e --test-id "..." --reason "..." --by you
testpulse quarantine list --suite admin-portal-e2e --format pytest-deselect
```

### Flake detection

Two classifiers run by default and they catch different things.

**same-commit** is close to proof: one commit that produced two different
outcomes. Either two runs of the same SHA disagreeing, or a single result that
was retried and went green (runners only retry a test that did not pass). Almost
no false positives, but it finds nothing unless your suite retries or CI runs a
commit twice.

**rolling-flip** needs neither. A test is flaky if its pass rate is between 0.05
and 0.95 *and* its flip rate is above 0.20. The flip gate is the important one:
a test that passed 30 times then failed 20 times has a pass rate of 0.6 but
flipped exactly once, which is a regression with a cause, not a flaky test.

Every threshold is config, never a literal in the code. Put a `testpulse.toml`
next to the database or set `TESTPULSE_FLAKE__FLIP_RATE_THRESHOLD` and friends.

### Quarantine

Quarantining is a human decision that gets recorded, not something derived from
the metrics automatically, and every entry expires (14 days by default). Expiry
does not re-enable the test or delete the entry. It just stops the list being
quiet about age, so a quarantine list cannot silently become a graveyard.

Exit codes are distinct because a CI step needs to tell them apart: `2` the report
could not be parsed, `3` this run was already ingested, `4` bad usage, `5` flaky
tests were found. `3` is frequently fine and `2` never is; `5` means the suite ran
and something in it is unreliable, which is not the same as a broken build.

## API

```bash
uv run uvicorn testpulse_api.main:app --reload
```

Interactive docs at `/docs`, schema at `/openapi.json`.

| Endpoint | |
|---|---|
| `GET /api/suites` | suites with stored runs |
| `GET /api/suites/{suite}/runs` | recent runs, newest first |
| `GET /api/suites/{suite}/health` | aggregate pass rate, flaky count, duration trend |
| `GET /api/suites/{suite}/tests` | paginated, sortable by any metric |
| `GET /api/suites/{suite}/tests/{test_id}` | metrics plus the full status timeline |
| `GET /api/suites/{suite}/flaky` | flaky tests with the evidence that fired |
| `GET /api/suites/{suite}/quarantine` | quarantined tests and quarantine debt |
| `POST /api/ingest` | upload a report, or a `.zip` for Allure |

Test detail is scoped under a suite because `test_id` is not globally unique, and
uses a `:path` converter because real ids contain slashes. Percent-encoding them
would be the obvious alternative and is unreliable — proxies commonly normalise
`%2F` back to `/` before the app sees the request.

`POST /api/ingest` has **no authentication**. It writes to the database. Run it
locally or behind something that authenticates until that is fixed.

## Dashboard

```bash
cd packages/testpulse-web && pnpm install && pnpm dev
```

React + Vite + TypeScript + Tailwind, Recharts for charts. Dark by default with a
light toggle. Five views: suite overview, flakiness leaderboard, slowest tests,
failure clusters, quarantine, plus per-test detail.

**The status timeline is the piece worth looking at.** One cell per run, oldest
first, so a flake pattern reads at a glance. Colour is never the only channel —
every cell carries a distinguishable glyph, because red/green is exactly the pair
a colourblind user cannot separate and it is the pair every test tool reaches for
first. A pass that only happened after a retry gets a third channel, an outline,
because that single cell is same-commit flake evidence on its own.

Accessibility is enforced, not assumed: axe runs in the component tests, and one
test asserts axe reports violations on broken markup so a green suite means
something. Verified against the running app in both themes: zero violations.

## CI integration

```yaml
- uses: Mohanad49/testpulse@main
  with:
    path: ./playwright-report.json
    format: playwright
    suite: admin-portal-e2e
    database-url: ${{ secrets.TESTPULSE_DATABASE_URL }}
    dashboard-url: https://testpulse.example/suites/admin-portal-e2e
```

The action ingests the report and comments on the pull request with **what
changed** — not what the state is. "47 tests failed" is useless in a PR because
45 of them were already failing on main; "2 tests started failing" is the line
that decides whether to merge. Pre-existing failures are counted and not listed.

The comment updates in place rather than appending, `fail-on-new` is off by
default (a tool that blocks merges on day one gets uninstalled), and an
"already ingested" result is treated as success so re-running a job is safe.

```bash
testpulse report --suite admin-portal-e2e --fail-on-new   # exits 5 if worse
```

## Running the whole thing

```bash
docker compose up --build
```

Dashboard on `:8080`, API on `:8000`. Postgres, migrations, API and web, with
migrations as their own one-shot service — an API that migrates on startup
cannot be scaled, and two replicas racing to migrate is a bug waiting to happen.

## Design notes

The reasoning behind the schema, the identity scheme and the parser behaviour is
in [DECISIONS.md](DECISIONS.md), including what was rejected and what is known to
be wrong.

Four things worth knowing before reading the code:

- **`test_id` stability is the load-bearing decision.** Everything in Phase 2
  joins on it. Playwright embeds line numbers in its identifiers, so they are
  stripped — otherwise editing a line above a test resets its history.
- **`retry_count` is nullable and `NULL` is not `0`.** `NULL` means the format
  cannot report retries. Collapsing them would make same-commit flake detection
  silently ineffective on formats that cannot answer.
- **An Allure results directory is not a run boundary.** It accumulates. The
  parser detects this and warns; it does not try to split it.
- **`flakiness_score` only describes the rolling-flip strategy.** A same-commit
  finding can score 0.00, so it can never be the only sort key.

## Testing

```bash
uv run --directory packages/testpulse-core pytest
uv run --directory packages/testpulse-api pytest
uv run mypy --strict packages/testpulse-core/src
uv run mypy --strict packages/testpulse-api/src
uv run ruff check .

cd packages/testpulse-web && pnpm test && pnpm typecheck
pnpm e2e     # Playwright against the real dashboard, incl. axe per view per theme
```

The API's contract tests validate real responses against the generated
`/openapi.json`, and one test checks that those contract tests are not a no-op —
a JSON Schema validator whose `$ref`s do not resolve accepts everything and
reports a green suite that checked nothing.

Fixtures are real reporter output, captured from real suites or generated by
running a throwaway one — never hand-written. A fixture written by hand encodes
the same assumptions as the parser it tests, so it passes while the parser fails
on real reports. See
[tests/fixtures/README.md](packages/testpulse-core/tests/fixtures/README.md) for
provenance.

## Not built yet

Phase 6 deployment and case study. Known gaps are listed at the end of each section in
[DECISIONS.md](DECISIONS.md).
