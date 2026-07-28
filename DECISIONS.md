# Decisions

Notes on why TestPulse is built the way it is. I'm writing these as I go, one
section per phase, because six months from now I won't remember why any of this
looked like a good idea.

Rule I'm following: if I chose something, I write down what I *didn't* choose and
what it costs me. A decision with no rejected alternative isn't a decision, it's
just the first thing I typed.

---

## Phase 1 — Ingestion and storage

### Parsers don't know the database exists

A parser takes a file path plus some CI metadata and hands back plain
dataclasses. Nothing else. The mapping onto SQLAlchemy rows happens in
`storage/repository.py`, which is the only file that knows both worlds.

The other option was letting parsers build ORM objects straight away. One less
mapping function to write. But then every parser test needs a database session
and a schema, and I can't change the storage layout later without editing four
parsers. Since I already know Phase 2 wants extra indexes and probably some
denormalised columns, that trade wasn't close.

What it costs: two model layers I have to keep in sync by hand. If they drift,
nothing catches it automatically. I'm accepting that for now because there are
only about a dozen fields.

### test_id is `file::class::name`, and I strip line numbers out of it

This is the single most important thing in Phase 1. Every metric in Phase 2 joins
on `test_id`, so if it isn't stable across runs, none of the rest works.

Playwright's Allure output identifies a test as
`recruitment/recruitment.spec.ts:65:7`. Line and column. Which means if I add an
import at the top of that file, every test below it gets a new identity and loses
its entire history, on a run where nothing about those tests changed. So I strip
any trailing `:line` or `:line:col`.

Two things I considered and dropped:

**Using the report's own ID.** Allure gives you a `historyId`. It's stable within
one framework, but it's computed by each framework's adapter, so the same test
gets a different hash if you switch runners. That's exactly the migration I did
at work (Selenium to Playwright), and losing all history at that moment is the
worst possible time to lose it.

**Fuzzy matching on names.** Tempting, and wrong. A bad merge joins two unrelated
tests and silently corrupts the numbers. A reset history just shows an empty
chart, and I can see that it's empty. Wrong data beats missing data at being
dangerous.

Still broken: rename a test, move its file, or rename its class and the old
history is orphaned. I'm storing `file_path`, `class_name` and `test_name` in
their own columns as well as the composite, so a later version can re-key the
existing rows without re-reading every report. Not doing it now.

### Four statuses, but I keep the original word too

Everything normalises to passed / failed / skipped / error, and the source
format's own wording goes into `raw_status` untouched.

The line I'm drawing: **failed** means an assertion didn't hold. **error** means
the test never got far enough to have an opinion. A dev fixes those differently
(one is your code, one is usually your fixtures or your environment), so I'm not
collapsing them. Allure's `broken` and JUnit's `<error>` are the same idea and
land on the same value.

Three mappings that are judgement calls, not facts:

- Playwright `timedOut` → **failed**. A timeout is nearly always the app being
  slow or stuck, and that's a product problem.
- Playwright `interrupted` → **error**. Somebody killed the run. The test never
  finished.
- pytest `xpassed` → **error**. This one's arguable. The test didn't fail, so
  "passed" is defensible. But an `xfail` marker on something that now works is a
  stale marker nobody's noticed, and calling it a pass buries it.

If someone pushes back on one of these in an interview it'll be `timedOut`, and
I think I'd still defend it.

### retry_count can be NULL, and NULL is not zero

`NULL` = this format has no way to tell me about retries. `0` = it does, and
there weren't any.

I nearly defaulted everything to 0. Would have been a quiet disaster. Phase 2's
best flake signal is "same commit, two different outcomes", and retries are the
cheapest source of that. If JUnit reports 0 retries instead of "no idea", the
strategy runs happily against JUnit data and finds nothing, and it looks like
there are no flaky tests instead of like there's no evidence.

Right now Playwright is the only format that answers. Allure technically records
retries, but as separate result files sharing a `historyId`, which needs
cross-file correlation. Pushed to Phase 2.

One thing that falls out of this and I want to remember: Playwright only retries
a test that didn't pass. So `retry_count > 0` **and** a final status of passed is
proof, on its own, that one commit produced two different outcomes. No extra
column needed to carry that forward.

### Re-ingesting the same file must not double the numbers

Natural key is (suite, commit, environment, started_at). Ingesting a duplicate
raises an error instead of quietly doing nothing, because "quietly did nothing"
and "worked fine" look the same from a CI step, and I'd rather the pipeline tell
me.

`started_at` being in the key means a real re-run of the same commit is *not* a
duplicate. It gets its own row. That's on purpose: Phase 2's same-commit strategy
lives on exactly those repeated runs, and collapsing them would delete the signal
I'm trying to detect.

**I got this wrong first.** The original version had an `allow_duplicate=True`
flag to store a second copy. It couldn't work, because the unique constraint
forbids the exact thing the flag promised. Caught it when the test failed with an
IntegrityError. Replaced it with `--replace`, which deletes the stored run and
writes the new one, and is what you actually want when you re-upload a fixed
artifact.

Hole I know about: if a report has no timestamps at all, the parser falls back to
"now", so every ingest gets a different `started_at` and the duplicate check
never fires. Formats without timestamps get no protection. Haven't solved it.

### Broken input gets rejected, not partly read

If a file won't parse, I raise. I don't return the rows I managed to read.

The case that convinced me is truncated JUnit XML. JUnit says a test passed by
*not* having a `<failure>` child element. So a file that got cut off halfway
parses into a shorter suite where everything still standing looks green. A
partial ingest there turns a failed upload into a clean run, which is about the
worst thing this tool could do.

Same reasoning for an empty Allure directory being an error rather than an empty
run. "The suite ran and had nothing to report" and "the results never got
written" are different problems and they look identical from here, so I refuse to
guess.

The cost is real: one corrupt file in a 500-file Allure directory kills the whole
ingest. I think that's the right side to err on for now.

### An Allure results folder is not one run

Found this by accident, which is the only reason I found it.

I pointed the CLI at the real `allure-results` folder in my orangehrm-playwright
repo to check the parser against something other than fixtures. It reported 124
results. Then I ran a query and there were only **18 distinct tests** in there.
About nine runs had piled up in that folder over a day, and the same test showed
up as passed, failed, skipped and error inside what my parser was cheerfully
calling a single run.

Every other format here is one file, written once, at the end of a run. The file
*is* the boundary. Allure has no boundary at all: the adapter drops one JSON file
per test into a folder and nothing ever cleans it up unless CI does.

So: detect it and warn, don't try to fix it. Splitting a folder into runs means
deciding what separates two runs, and I don't have a good answer yet (a time gap?
a repeated test name? both are guesses). That belongs with the metrics work. The
warning at least means it can't happen silently.

This has a knock-on for Phase 6 that I need to remember: the demo can't be seeded
from a folder that's been accumulating locally, or the run-level numbers are
meaningless. The CI workflows in my other repos need to clear the directory
between runs first.

### Fixtures are real reporter output, never hand-written

Every fixture in `tests/fixtures/` is something a real tool actually emitted.
Most captured from my existing suites; the rest generated by running a throwaway
suite and keeping the reports as-is.

Reason: a fixture I write by hand encodes the same assumptions as the parser I'm
writing. It passes, and then the parser falls over on a real report. I've done
this to myself before.

Three things real files caught that I would not have thought of:

1. **Allure attachments live inside steps, not on the result.** I checked all 270
   real result files: zero had a top-level `attachments` entry, 47 had them
   nested inside the step tree. Reading `document["attachments"]` gives you an
   empty list for every single test and looks completely fine, because "no
   screenshots" is a believable answer.

2. **Allure's `fullName` is a different format depending on which adapter wrote
   it.** Playwright writes `spec.ts:65:7`, pytest writes
   `pkg.Module.Class#test_name`. There's no parsing rule that handles both
   without first guessing who produced the file. That's why identity comes from
   the labels instead.

3. **pytest-json-report's `created` field is the END of the session.** It reads
   like a start time and I used it as one. Caught it by comparing against a JUnit
   report from the same pytest run: `created - duration` matched that report's
   timestamp to six decimal places. Used as a start time it shifts every run
   forward by however long the suite took, which you'd never notice on a fast
   suite and would completely scramble a slow one.

Point 3 only got caught because two reports of the same session existed. Most of
this parser set has no second source to check against, which bothers me a bit.

### Nothing from the Blue Ribbon repo goes in here

`blue-ribbon-qa-automation` has real module names, test names and failure
messages from work in it. None of that goes into a public repo, as fixtures or as
demo seed data. Not negotiable, including later when the demo looks thin.

### Left open after Phase 1

- Rebuilding Allure retry counts by correlating `historyId` across files.
- Working out what actually separates two runs inside one accumulated folder.
- JUnit's `finished_at` is currently `started_at + sum of test durations`, which
  is wrong for anything running in parallel. It undercounts. Might not matter
  until the duration charts exist.

---

## Phase 2 — Metrics and flake detection

### Every threshold lives in config, none of them in code

`testpulse.toml` first, environment variables override it, defaults are in
`config.py` with the reasoning written next to each one.

This isn't a style thing. "Is this test flaky" is a policy question, not a fact.
Two teams can look at the same numbers and disagree about where the line goes,
and both can be right for their situation. A threshold hardcoded in a function is
a policy that one team imposed on everyone and that nobody can find later when
they want to argue with it.

The order matters too: env beats file. CI needs to loosen a threshold for one job
without touching a committed file, and the committed file needs to be the shared
default everyone gets.

### Why 0.05, 0.95 and 0.20

These are the numbers I'll get asked about, so writing out the actual reasoning.

**Lower bound 0.05.** A test that fails every single time is not flaky, it's
broken. It's completely predictable. Putting it on a flakiness leaderboard buries
the tests that are genuinely non-deterministic under a pile of tests that just
need fixing.

**Upper bound 0.95.** Over a 50-run window that's one failure. One failure in
fifty is a bad night, not a pattern. Without this bound every test that ever
hiccuped once shows up as flaky forever until the window rolls past it.

**Flip rate 0.20.** This is the one that does the real work and it's the one I'd
lead with. Take a test that passed 30 times and then failed 20 times in a row.
Pass rate 0.6, sitting right in the middle of the band, so both bounds let it
through. But it flipped exactly once out of 49 opportunities. That's not flaky,
that's a regression with a specific cause somebody introduced on a specific
commit, and calling it flaky sends someone hunting for a race condition that
doesn't exist. The flip gate is what separates "intermittent" from "recently
broke".

### Two strategies, because each one is blind to what the other sees

**Strategy A, same-commit disagreement.** If the same commit produced two
different outcomes, the code didn't change and the outcome did. That's close to
proof rather than inference. Two sources of it: two runs against one commit_sha
that disagree, and a single result with `retry_count > 0` that ended up passing.
The second one falls out of the Phase 1 decision to make `retry_count` nullable,
and it's the reason that decision mattered. Runners only retry a test that didn't
pass, so one passing result with retries means the same binary produced both
outcomes minutes apart.

Almost no false positives. Also finds nothing at all unless your suite retries or
your CI runs the same commit twice, which is why it can't be the only one.

**Strategy B, rolling flip rate.** Works on any suite with history, needs no
retries and no repeated commits. Costs precision: a test failing because of a
real intermittent bug in the product looks exactly the same as a badly written
test from here. That's a limit of the approach, not something I can fix with a
better threshold.

Which strategies run is configurable, because running only A is a legitimate
choice for a team that retries everything and doesn't want inference.

### flakiness_score is `flip_rate * 4p(1-p)`

Needed a single number to sort the leaderboard by. The `4p(1-p)` part is a
parabola that peaks at 1.0 when the pass rate is 0.5 and hits 0 at both ends. So
an always-failing test scores 0 no matter what, which is right, because it's
predictable.

Multiplying by flip rate separates two things that have the identical pass rate
of 0.5 and are completely different problems: a test that alternates every run,
and a test that passed 25 times then failed 25 times.

It's a ranking aid, not a probability. Don't let anyone read it as one.

### A same-commit finding can score 0.00, and that broke the sort

Caught this when I ran the real Playwright report through it. The test that
failed and passed on retry got flagged flaky by Strategy A, correctly, from a
single run. Its pass rate was 100% and its flip rate was 0, so its flakiness
score was exactly 0.00, so it sorted to the **bottom** of the leaderboard, below
tests with no evidence against them at all.

The score only describes Strategy B. It can't describe Strategy A, because
Strategy A isn't a rate. So the sort is `is_flaky` first, then score. Obvious in
hindsight and completely invisible until I put real data through it.

### Minimum run count before Strategy B is allowed to speak

Added after a test caught it firing on a two-run history. One pass and one fail
gives pass rate 0.5 and flip rate 1.0, which sails through every threshold. But a
test that has run twice and failed once is much more likely a regression somebody
should look at than a flaky test to quarantine.

Default is 5. Strategy A is deliberately not gated the same way, because its
evidence doesn't get stronger with repetition. One same-commit disagreement
already tells you everything.

### Skipped results are dropped before every metric

A skipped test didn't pass and didn't fail. Counting it either way is a lie, and
counting it in the denominator is the worse lie: a test skipped for 40 of 50 runs
would show a pass rate of 0.2 despite never having failed once.

Related: an all-skipped test gets `pass_rate = None`, not 0. Zero would sort it
next to tests that genuinely fail every time, which is a completely different
situation.

### Flip rate compares pass/not-pass, not the four statuses

A test going from failed to error hasn't flipped in any way a human cares about.
It failed both times, for a slightly different reason. Counting that as a flip
would rank tests with unstable *failure modes* above tests that are actually
non-deterministic, which is exactly backwards.

### p95 uses nearest-rank, not interpolation

The answer is always a duration some run really produced. On a 50-run window the
p95 is the 48th value, and interpolating between two samples invents a number
that never happened. "This test takes 4.2s at p95" is a lot easier to defend when
some run really did take 4.2s.

### is_newly_failing needs a spotless history

Three conditions: a current failure streak of at least 2, at least 3 runs before
that streak, and those earlier runs all passed.

The last condition is what keeps this from overlapping with flakiness. If the
history before the streak was already mixed, the test was flaky and has now
tipped over, and that's a different conversation than "this broke on Tuesday".
Two runs of a brand new test failing isn't a regression either, because there's
nothing to regress from, which is what the minimum-prior-runs condition handles.

### The window is counted in runs, not days

A suite that runs 40 times on Monday and once on Saturday has wildly different
amounts of evidence inside a seven-day window. Every threshold in the config is a
fraction of runs, so if the denominator isn't runs the thresholds stop meaning
anything consistent.

Branch filtering is optional but usually what you want. Mixing a long-lived
feature branch into main's history imports failures that were never main's
problem, and you end up with flake numbers describing a branch nobody runs.

### first_seen_at is not windowed

"When did this test first appear" is a question about the test's whole life. If I
answered it from the window, every long-standing test would look brand new the
moment the window rolled past its origin.

### Quarantine is a human decision, and it expires

Two positions here, both arguable.

**It's not automatic.** The classifier proposes candidates, a person records the
decision, and their name goes on it. Auto-quarantining everything the classifier
flags means tests silently stop gating merges because a number crossed a
threshold at 3am and nobody is accountable for it. I've seen the version of this
where nobody knows why a test is disabled and I'd rather it be somebody's call.

**Everything expires, default 14 days.** A quarantine list without expiry turns
into a graveyard. Tests get disabled during a bad week, the incident passes,
and two years later there are sixty skipped tests and nobody knows whether the
code they covered still works. The expiry deliberately does *not* delete the
entry or re-enable the test. It just makes the list stop being quiet about age.
That surfaced debt is the entire point of the feature.

Days remaining is stored signed, so an overdue entry reports "expired 47 days
ago" rather than just "expired". The number is what actually gets people to act.

Re-quarantining resets the clock instead of adding a second row. That's a real
decision being made a second time ("yes, still broken, another two weeks") and it
should show as a new date.

### The export formats, and where they lose

`--format json` is the complete picture. The other two are for wiring into CI and
both give something up.

**pytest.** The brief called for markers. Markers turn out to be the wrong tool:
a marker has to be written into the source file next to the test, so applying one
from an external list means either editing test files from CI or writing a
conftest hook that reads the list and rewrites collected items. `--deselect`
takes a nodeid on the command line and needs nothing installed, so that's what I
emit. What I give up: a deselected test vanishes from the report entirely, where
a marked one could show as skipped-because-quarantined. If that visibility turns
out to matter, the conftest hook is the upgrade path.

There's also a case it can't handle at all. Our `test_id` is already nodeid
shaped, but only when the report gave us a file path, and JUnit usually doesn't.
Those come out as `::SomeClass::test_name`, which pytest can't deselect. I emit a
comment line naming them rather than an argument that would fail quietly at
collection time.

**playwright.** Playwright greps on the test title, not a file path, so this can
only use the name segment. Titles get regex-escaped because test names are full
of brackets and parentheses, especially parametrised ones. The imprecision I
can't avoid: two tests in different files with the same title are
indistinguishable to a title regex, so quarantining one skips both. Worth knowing
before anyone wires it into a pipeline.

Also: an empty list has to produce an empty string, not an empty alternation. An
empty regex in `--grep-invert` matches everything and skips the whole suite.
There's a test pinning that.

### Property tests, because I don't trust my own examples

The example-based tests cover cases I thought of. The Hypothesis ones cover the
ones I didn't. The important one is that a test which always passes is never
classified flaky, under any strategy, for any length of history. If that ever
breaks, every number this tool produces is suspect.

The others are boundary invariants: rates stay in 0-1, the p95 is always a real
observed duration, a test that never failed has no failure signals, and
`is_newly_failing` always implies a current failure streak.

### Left open after Phase 2

- Allure retries still aren't reconstructed, so Strategy A can't use Allure data.
  Needs `historyId` correlation across files.
- Failure-message clustering (grouping 40 failures with one root cause) is listed
  under the Phase 4 dashboard, but the clustering itself belongs down here in the
  metrics layer. Will probably move it.
- Nothing recomputes metrics incrementally. Every call recomputes the whole
  window from raw rows. Fine at this size, will need caching before the API is
  serving a dashboard.

---

## Phase 3 — API

### Response models are hand-written, not generated from the ORM

There's a `schemas.py` full of Pydantic models that partly duplicate the
SQLAlchemy entities. That duplication is the point.

The database shape and the API shape are allowed to drift, and they already have.
The API returns computed metrics that aren't in any table, and it hides things
like the full failure stack that would make a list response enormous. If I
generated these from the ORM, every future column rename would be a breaking
change to a published contract, and I'd find out from a consumer.

Cost: two definitions to update when a field changes. Accepted, same as the
parser/ORM split in Phase 1, for the same reason.

### The test detail route is scoped under a suite, and uses a `:path` converter

The endpoint list I started from had this as a flat `/api/tests/{test_id}`. Two
things wrong with that.

**`test_id` isn't globally unique.** `tests/a.py::Cls::test_login` can exist in an
admin suite and a mobile suite at the same time. A flat route would merge two
different tests' histories into one chart and nothing would look broken. So the
route is `/api/suites/{suite}/tests/{test_id:path}`.

**`test_id` contains slashes.** Real ones look like
`recruitment/recruitment.spec.ts::Recruitment Tests::Delete a vacancy`. The
obvious fix is percent-encoding the id, and percent-encoded slashes are a trap:
plenty of proxies normalise `%2F` back to `/` before the app ever sees the
request, so the encoding works locally and silently stops working in the
deployment where it matters. The `:path` converter takes the rest of the URL as
one segment and sidesteps the whole thing.

(Spaces still need encoding, obviously. I wasted a few minutes on a 000 from curl
before working out it was my shell command and not the server.)

### Sorting is an allowlist, not `getattr`

`/tests?sort_by=` maps through a fixed dict. The lazy version is
`getattr(metric, sort_by)`, which lets a request sort by any attribute that
happens to exist on the object, and turns a typo into a 500 instead of a 422.
Neither is catastrophic on a read-only metrics endpoint, but "user input reaches
getattr" is the kind of thing I'd flag in someone else's code review.

Nulls sort last in both directions. A test with no pass rate hasn't scored well or
badly, and letting it float to the top of an ascending sort puts "no data" above
"worst", which is the wrong answer to the question the user asked.

### The paginated endpoint doesn't actually reduce work, and says so

`/tests` computes metrics for the whole suite in Python and then slices. Paging
doesn't make the request cheaper. It can't: the sortable fields are computed
metrics that don't exist in any column, so the sort can't be pushed into SQL.

I'm leaving it. At portfolio scale it's irrelevant, and the honest fix isn't a
cleverer query, it's a materialised metrics table refreshed on ingest. Writing
that now would be building for a load that doesn't exist. The docstring says all
of this so nobody has to discover it with a profiler.

### `/health` doesn't touch the database

The liveness endpoint returns a constant. It's tempting to have it run a `SELECT
1`, and that's how you get an orchestrator killing a perfectly healthy API
because a database failover took four seconds. Liveness and readiness are
different questions. This one answers liveness.

### The upload endpoint, and the part I actually thought hardest about

Allure results are a directory, so accepting them over HTTP means accepting a
zip, and unpacking an archive from an untrusted caller is the most dangerous
thing in this codebase. Four guards, each for a different attack:

**Path traversal (zip slip).** An entry named `../../../etc/cron.d/x` writes
outside the extraction directory. Python's `extractall` does sanitise this now,
but I check the resolved path anyway, because I'd rather assert the property I
want than rely on the current behaviour of a stdlib function.

**Symlinks.** A zip can carry a symlink entry pointing anywhere, and a later
entry writes *through* it. The resolved-path check doesn't catch this, because
the symlink itself lands legitimately inside the destination. Symlink entries get
rejected outright. This is the one people miss.

**Decompression bombs.** A few hundred KB can expand to gigabytes, so an upload
size limit constrains nothing about what unpacking costs. Checked against the
declared uncompressed size in the headers, before writing a byte.

**Entry count.** Millions of tiny files exhaust inodes and time without ever
tripping a size limit, so that's its own gate.

All four have tests, and the malicious archives are built inside the tests rather
than committed — committing a zip bomb to a public repo is a good way to get the
repo flagged.

**No auth.** That's a real gap, not an oversight. This endpoint writes to the
database and anyone who can reach it can write to it. Fine while it's local or
behind something that authenticates; has to be closed before the instance is
public. It's written in the docstring so it can't be forgotten quietly.

### 409 for a duplicate upload, not 400

The request was well formed. It conflicts with what's already stored. A client
can do something useful with that distinction ("already ingested, carry on")
and can't do anything useful with a generic 400. Same reasoning as the separate
CLI exit codes in Phase 1.

### One error shape everywhere

Every non-2xx returns `{"detail": "..."}`. One shape means a consumer writes one
error path instead of one per endpoint. There's a test that walks several error
routes and asserts the body has exactly that one key.

### The engine has to arrive by injection, including on the write path

Caught this when the ingest tests all failed with "no such table". The handler
was calling `get_engine()` directly instead of taking it as a dependency, so
FastAPI's `dependency_overrides` couldn't reach it, and the write path was
pointed at the real configured database while the reads used the test one.

In a test that's a confusing failure. In production it's writes and reads
disagreeing about which database they're using, with nothing complaining. Fixed
by adding an `EngineDep` and injecting it like everything else.

### Contract tests validate against the published document, not my expectations

FastAPI generates the OpenAPI doc from the response models, so in theory they
agree by construction. In practice the gap opens when a handler returns something
FastAPI coerces, or a field is null in reality and non-nullable in the schema,
or an error path returns a shape nobody declared. That gap is exactly what breaks
a generated client.

So the tests pull each response schema out of `/openapi.json` and validate real
responses against it with `jsonschema`.

**And there's a test that the tests aren't a no-op.** This mattered. My first
attempt registered the spec at the wrong base URI and every `$ref` dangled. That
version happened to raise, which is lucky, because a *subtly* wrong base gives
you a validator that resolves nothing, accepts everything, and reports a
beautiful green contract suite that checks precisely zero things. So there's a
test that feeds the validator a deliberately broken payload and insists it
complains. A test tool whose own contract tests are decorative would be a bad
look in an interview and worse in real life.

Two schema details get their own assertions because they're the most likely to
break a generated client: `pass_rate` must be declared nullable (an all-skipped
test really does return null), and `flake_evidence` must serialise as an array of
strings rather than leaking the tuple it is internally.

### Config is read once per process

Flake thresholds change the meaning of every number the API returns. If they were
re-read per request, two calls in the same second could answer the same question
differently after someone edited a file. Restarting to pick up a config change is
the honest behaviour.

### `py.typed` was missing and nothing told me

`testpulse-core` had no `py.typed` marker, so as soon as `testpulse-api` imported
it, mypy treated the whole package as untyped and silently stopped checking any
of those calls. A strict-mode library that ships no marker is a library whose
types nobody downstream can use. Added to both packages.

### Left open after Phase 3

- No auth on `/api/ingest`.
- The API 500s with a raw SQL error if the database is behind on migrations. It
  should check the alembic version at startup and refuse to serve, rather than
  failing per-request with something unreadable. Hit this myself with a stale
  local database.
- No pagination on the timeline in test detail; it's capped at 500 points and
  that's it.
- No caching. Every metrics request recomputes the window from raw rows.
