# Deploying TestPulse

Everything here is free with no card. Two accounts: **Neon** (Postgres) and
**Vercel** (static hosting), both sign in with GitHub.

There is no application server. CI writes real runs to Neon, exports a static
snapshot, and pushes it to Vercel. That is what makes it permanently free — see
DECISIONS.md for why a hosted API was rejected.

Total time: about 15 minutes.

---

## 1. Neon — the database (5 min)

1. Go to **https://neon.com** and sign in with GitHub. No card is requested.
2. **Create a project.** Name it `testpulse`. Pick the region closest to you
   (`eu-central-1` from Cairo). Postgres version default is fine.
3. On the dashboard, find **Connection string** and copy it. It looks like:

   ```
   postgresql://neondb_owner:npg_XXXX@ep-something-123456.eu-central-1.aws.neon.tech/neondb?sslmode=require
   ```

4. **Change the scheme.** SQLAlchemy needs the driver named, so replace
   `postgresql://` with `postgresql+psycopg://`:

   ```
   postgresql+psycopg://neondb_owner:npg_XXXX@ep-something-123456.eu-central-1.aws.neon.tech/neondb?sslmode=require
   ```

   Keep `?sslmode=require`. Neon rejects unencrypted connections.

Optional sanity check from your laptop:

```bash
cd ~/Coding/testpulse
export TESTPULSE_DATABASE_URL='postgresql+psycopg://...'   # the edited string
uv sync --all-extras
cd packages/testpulse-core && uv run --project .. alembic upgrade head
cd ../.. && uv run testpulse suites     # prints nothing yet - correct, it is empty
```

If that runs without error the string is right. CI migrates on its own, so this
step is only to catch a typo now rather than in a scheduled run at 3am.

---

## 2. Vercel — the dashboard (5 min)

1. Go to **https://vercel.com**, sign in with GitHub.
2. **Add New → Project**, import `Mohanad49/testpulse`.
3. Configure:
   - **Root Directory**: `packages/testpulse-web`
   - **Framework Preset**: Vite
   - **Build Command**: `pnpm build`
   - **Output Directory**: `dist`
   - **Environment Variables**: add `VITE_DATA_MODE` = `static`
4. Click **Deploy**. The first deploy will show the empty state, because no
   snapshot exists yet. That is expected.
5. Collect three values for GitHub:
   - **Token**: avatar → Account Settings → Tokens → Create. Scope: Full Account.
   - **Project ID** and **Org ID**: Project → Settings → General, near the bottom.

---

## 3. GitHub secrets (2 min)

Repo → **Settings → Secrets and variables → Actions → New repository secret**.
Four secrets:

| Name | Value |
|---|---|
| `TESTPULSE_DATABASE_URL` | the edited Neon string from step 1 |
| `VERCEL_TOKEN` | from step 2 |
| `VERCEL_ORG_ID` | from step 2 |
| `VERCEL_PROJECT_ID` | from step 2 |

Or from the terminal:

```bash
cd ~/Coding/testpulse
gh secret set TESTPULSE_DATABASE_URL   # paste when prompted
gh secret set VERCEL_TOKEN
gh secret set VERCEL_ORG_ID
gh secret set VERCEL_PROJECT_ID
```

---

## 4. Run it (2 min)

```bash
gh workflow run ci.yml
gh run watch
```

The `Ingest our own results` job now migrates Neon, ingests this run, exports the
snapshot and deploys it. Your dashboard is live at the Vercel URL.

**It will look thin at first**, and that is the honest behaviour rather than a
bug: `rolling-flip` refuses to classify anything under 5 scored runs. After five
nightly runs the flakiness view starts having something to say. The five
schedules (TestPulse plus your four QA repos) run at 02:00–06:00 UTC.

---

## 5. Make the repo public

Repo → Settings → General → bottom → **Change visibility → Public**.

Check first that nothing private slipped in:

```bash
git log --all --oneline | wc -l
git grep -iE "npg_|vercel_|password|secret" -- . ':!DEPLOYMENT.md' ':!DECISIONS.md' | head
```

Both secrets live only in GitHub Secrets and are never written to a file, so this
should come back clean.

---

## Notes on vercel.json

Two things in there that are not obvious:

- **The rewrite excludes `/data/` and `/assets/`.** Everything else falls through
  to `index.html` because `test_id`s contain slashes, so a deep link to one test
  has many path segments and every one of them has to reach the app rather than
  404. `/data` and `/assets` are real files and must not be rewritten.
- **`/data/` is cached for five minutes, not a year.** The snapshot is
  regenerated nightly, and a long cache would serve yesterday's numbers all day.

There are no `"//"` comment keys in that file. JSON has no comments, and Vercel
validates the schema strictly — an unrecognised property fails the deploy with
`should NOT have additional property`.

## What this does not deploy

**The API is not hosted.** It is what a self-hosted install runs
(`docker compose up`), it is covered by 68 tests, and the static exporter is a
second consumer of the same query layer. It is not on the public internet because
a free host would either sleep for 50 seconds before the first response or
withdraw its free tier, and because `POST /api/ingest` should not be exposed
publicly regardless.

**Cost check.** Neon free: 0.5 GB storage, scale-to-zero, no card. This workload
is a few writes a night and one read at export. Vercel free (Hobby): static
hosting, no card. GitHub Actions: free minutes on public repos are unlimited, so
making the repo public in step 5 also removes any minutes concern.
