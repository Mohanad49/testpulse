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

---

## Phase 4 — Dashboard

### I overrode most of what the design tool told me

I ran the UI/UX skill on this and its headline answer was "Real-Time /
Operations **Landing**" with a style called Exaggerated Minimalism:
`clamp(3rem, 10vw, 12rem)` headings, massive whitespace, a light blue-and-amber
palette. That is a marketing page for an ops product. It is close to the exact
opposite of what this needs, which is a dense data display that gets read next to
a terminal.

I kept what was actually useful and threw out the rest:

- **Kept** the developer-tool dark palette from its colour database (slate,
  `#0f172a` background), which is right.
- **Kept** its chart guidance, and one line from it shaped the whole phase — see
  the timeline decision below.
- **Rejected** the style and the layout. Density dial to 9, spacing scale down to
  4px steps.
- **Rejected** the typography too, though that one is closer. It suggested Fira
  Code / Fira Sans, which is a genuinely fine dashboard pairing. I went with
  Inter + JetBrains Mono because the brief names Linear and Vercel Analytics as
  the target feel and that is their stack.

Worth writing down because "I used the tool" and "I did what the tool said" are
different things, and the second one is how you end up with a portfolio piece
that looks like everyone else's.

### The status timeline, and why colour is never the only channel

This is the signature visual: one cell per run, oldest left, so a flake pattern
reads without a single number.

The line from the chart guidance that changed my approach: *differentiate by
shape, not colour alone*. Obvious once stated, and I had not thought about it
properly. Roughly one man in twelve has a colour vision deficiency, red/green is
precisely the pair they cannot separate, and red/green is the first thing every
test tool reaches for. So a strip of red and green cells is unreadable to a
meaningful slice of the people it is for.

Every cell therefore carries a glyph as well as a colour, and the glyphs are
distinct in monochrome: `▍` passed, `✕` failed, `!` error, `–` skipped at half
height. Screenshot it into a report, print it greyscale, or view it with
deuteranopia and the pattern still reads.

There is a third channel for one specific case. A test that failed and then
passed on retry is the single most important cell in the whole visual, because
it is same-commit flake evidence in one square. It is the same green as an
ordinary pass, so it gets an outline as well, and its accessible name says
"passed on retry" out loud.

**Keyboard.** The strip is one composite widget with arrow-key navigation, not
50 focusable buttons. Fifty tab stops to cross one table row would be a worse
experience than no keyboard support at all.

**No tooltip.** Detail appears in a fixed line below the strip. A hover tooltip
on a 10px target is unusable with a trackpad and invisible to a screen reader.

### Rows expand in place

The leaderboard expands a row into its run history rather than opening a modal or
navigating to a detail page. The actual job on that screen is comparing patterns
between two flaky tests, and both alternatives hide one of the two things you are
comparing.

### Slowest is ranked by p95, not mean

A test that usually takes 200ms and occasionally takes 40 seconds has an
unremarkable mean and is the reason CI is slow. The mean is shown next to it so
the gap is visible, and a p95 more than double the mean gets tagged "spiky",
because that is a different problem from "slow" and needs a different fix.

### Things real data broke that fixtures never would have

I pointed it at the real ingested runs and immediately found four things:

1. The pass-rate axis read **"00%"**. `width={44}` was clipping "100%".
2. The duration axis wrapped to `1000m 0s` because I reused the full duration
   formatter on the axis. Axes now get a one-unit compact format; the tooltip
   keeps the precision.
3. Every X tick read **"May 30"**. CI runs many times a day, so a date-only label
   is a column of identical strings. It now checks the actual span of the data and
   switches to clock time inside a day.
4. The trend read `▼ −282012ms/run`, which is technically correct and useless.

None of these show up on invented data with tidy round numbers spread over weeks.

### The accessibility scan found two real bugs, and one thing that was not a bug

axe runs in the component tests, and there is a test asserting axe reports
violations on deliberately broken markup — same reasoning as the API contract
tests. A green a11y suite proves nothing if the checker silently is not running.

Scanning the actual running page found two things the component tests missed:

**`aria-controls` was invalid, flagged critical.** I had used the `test_id` as an
element id. A `test_id` looks like
`leave/leave-management.spec.ts::Leave Management Tests::Apply for leave (or ...)`
— spaces, slashes, colons, parentheses. Not a valid HTML id, so `aria-controls`
pointed at nothing. Now uses the row index.

**`--fg-subtle` failed contrast**, measuring 3.75:1 on the page background where
4.5:1 is required. Lifted it. The consequence is that the "subtle" tier is now
much closer to the "muted" tier than I designed it to be, and I am taking that
trade: AA sets a floor on how quiet text is allowed to be, and hint text nobody
can read is a bug rather than a design choice.

**And one thing I got wrong, corrected in Phase 5.** A scan appeared to show
five contrast failures in light mode, reporting a dark-mode foreground against a
light-mode background — a combination that cannot exist. I diagnosed it as a
stale style recalculation caused by flipping `data-theme` from the console, and
concluded the light-mode failures were an artifact.

**The specific mixed-token reading was an artifact. The conclusion that light
mode was clean was not.** The Phase 5 Playwright suite, loading each theme
properly from `localStorage` on a fresh page, found real contrast failures in
*both* themes. See the Phase 5 section. The lesson is not "trust the scan" but
"a console scan of a page I hand-mutated is not the same instrument as a scan of
a page loaded normally", and I stopped at the first explanation that fitted.

### No data-fetching library

Ten read requests, no mutations, no cache invalidation. react-query would be more
machinery than it removes at this size. The one thing that genuinely matters is
aborting in-flight requests when the suite changes, because otherwise a slow
response for the previous suite lands after a fast one for the new suite and
silently renders the wrong data. That is about fifteen lines.

### The suite lives in the URL

Not in component state. The point of this tool is sending somebody a link to the
flaky test you want them to look at, so every view has to be linkable and the
back button has to work.

### Seeding the demo, and an honest note about it

The screenshots come from real runs of my own suites, not invented data. Getting
there needed one piece of work worth admitting to.

The `orangehrm-playwright` allure directory holds 124 result files that are
really about 16 accumulated runs — the Phase 1 finding. To show trends I had to
split it back into runs, which I did with a one-off seeding script that starts a
new run whenever a test id it has already seen appears again (a test cannot
execute twice in one run). That heuristic lives in the demo seeding, **not** in
the parser, because it is a guess and the parser should not guess.

It is not perfect: one recovered "run" spans an implausible wall-clock time
because results interleaved across a gap, which is why one duration bar dwarfs
the rest. That is an artifact of the reconstruction, not of the product, and the
right fix is CI clearing the results directory between runs.

### Left open after Phase 4

- The bundle is ~640KB (190KB gzipped), most of it Recharts. Fine for a
  dashboard behind a login, worth code-splitting before it is a public demo.
- No virtualisation on the tables. At a few hundred tests that is fine; at ten
  thousand it will not be.
- The quarantine view is read-only. Adding and removing is CLI-only, which is
  defensible (quarantine is a deliberate act with a name attached) but it does
  mean a round trip to a terminal.
- No E2E test of the dashboard itself. Playwright against the running app is
  Phase 5 work, and there is a pleasing symmetry in this project's own dashboard
  being tested by the tool the dashboard reports on.

---

## Phase 5 — CI integration

### The PR comment reports changes, not state

This is the whole design. A pull request comment gets about four seconds of
attention from someone deciding whether to merge, and "47 tests failed" spends
all four of them saying nothing useful — 45 of those were already failing on main
and have nothing to do with the change under review.

So the comment reports the diff: what started failing, what went flaky, what got
slower, what recovered. Pre-existing failures are **counted and deliberately not
listed**. Listing them is how the comment becomes a wall of red that everyone
learns to scroll past, and then the two lines that mattered scroll past with it.

Everything is computed against the window *excluding* the run being reported on.
Include it and the run dilutes its own signal: one failure in a window of one is
a 0% pass rate; one failure in a window of fifty barely moves.

"Newly flaky" genuinely means newly. The classifier runs twice — once on the
history, once on the history plus this run — and only a change between the two
is reported. Flaky before and flaky now is not news.

### The bug real output found

I ran the report against the real ingested data and read what came out. It said:

> 🔴 Started failing — `Delete a vacancy` — passed 0% of the last 8 runs

Which is self-contradicting. A test that passed 0% of its last 8 runs did not
start failing today. The cause: I was checking `prior[-1].is_failure` to decide
whether a test was already broken, and the most recent entry in that test's
history happened to be a **skip**. A skip is not a recovery, but it reset the
judgement and a long-broken test got filed as a fresh regression.

Now it looks at the last observation that actually *ran*. Two tests cover both
directions — a skip must not hide a real new failure either.

I would not have caught this from the unit tests I had written, because I wrote
them against the behaviour I intended.

### Duration regressions need a floor more than a ratio

50% slower than the prior p95, and never for anything under 500ms.

The floor is doing most of the work. Without it, a test going from 2ms to 6ms is
a "200% regression" and the section fills with the fastest tests in the suite
while the genuinely slow ones get truncated off the bottom. The 50% is loose on
purpose too: CI runners vary that much between jobs, and a comment that fires on
noise gets muted within a week. A muted comment is worse than no comment, because
everyone still believes something is watching.

### The action is composite, not Docker

Docker actions only run on Linux runners and pay a container pull per job. The
suites that would feed this run on macOS and Windows as well, so a Docker action
would exclude exactly the platforms where cross-platform flakiness lives.

Three behaviours worth defending:

**Exit code 3 is success.** That is the "already ingested" code. Re-running a
failed job should not fail because ingest correctly refused to write a duplicate.

**The comment updates in place.** It carries a hidden HTML marker and the action
looks for it before posting. Twenty stale bot comments on a long-lived PR is how
a useful tool turns into noise people collapse by default.

**`fail-on-new` defaults to false.** A tool that starts blocking merges the day
it is installed gets uninstalled the day after. Adopt it, trust it, then turn the
gate on.

### TestPulse's own CI runs nightly, and that is not decoration

The schedule exists because the same-commit flake strategy needs repeated runs of
one unchanged SHA, and a push-only trigger never produces any. A suite that only
runs when somebody pushes has no way to distinguish a flaky test from a broken
one, which is precisely the problem this project exists to solve.

Concurrency cancellation is enabled on pull requests and disabled on main and on
the schedule. On a PR a superseded run is waste; on main every run is a data
point the flake detection is counting.

The E2E suite runs with `retries: 2` in CI. Not to paper over flakiness — to
*produce* retry data, which is the one input the high-precision strategy can
read. TestPulse ingesting its own Playwright report closes the loop: a tool that
cannot detect flakes in its own test suite is not much of a tool.

### Self-ingest only on main

On a pull request the history would fill with runs from branches that may never
merge, and the flake numbers would end up describing code nobody is running.

### The E2E suite mocks nothing

It drives the real dashboard against the real API over a seeded database. The
component tests already cover components in isolation; a mocked API at this level
would only prove the mock matches the mock. The seam between dashboard and API is
the entire reason these tests exist.

Seeded from the committed fixtures rather than generated data, so assertions can
be exact. "Some tests are listed" passes on a broken page. "These tests are
listed" does not.

**And it immediately found a real bug.** Vite's `server.proxy` applies only to
`vite dev`. The E2E suite runs against `vite preview`, which needs `preview.proxy`
configured separately — without it every API call 404s and the suite is testing a
broken application while looking like it works. Nothing in the unit tests or the
manual browsing would ever have surfaced that, because both used the dev server.

### Accessibility scans moved up a level

Phase 4 had axe running over components. That stays, but the E2E suite now runs
axe per view per theme against the fully rendered page — ten scans. Both real
accessibility bugs found while building the dashboard (an invalid `aria-controls`
and a contrast failure) were only visible at page level. Component scans cannot
see landmark structure, heading order, or contrast against the real background.

### Migrations are their own compose service

Not the API's entrypoint. Two API replicas both running migrations at boot is a
race, and an API that migrates on start cannot be scaled. As a one-shot service
with `service_completed_successfully`, the API simply does not start until the
schema is ready.

The API waits on `pg_isready`, not on the container starting. Postgres accepts
TCP connections several seconds before it will answer a query, so "the port is
open" and "the database is ready" are different facts.

### The Docker image runs as a non-root user

This is the process that unpacks archives uploaded by callers. It is the one part
of the system where being wrong about a path has consequences, and the four
extraction guards are easier to trust with a uid that cannot write anything
interesting.

`psycopg` is an optional extra rather than a base dependency. Most CLI use is
SQLite, and shipping a compiled driver to people who will never open a Postgres
connection is cost with no benefit.

### Left open after Phase 5

- The action installs TestPulse from git rather than PyPI. Fine for a portfolio
  piece, wrong for anything anyone else depends on — publishing is a Phase 6
  decision.
- `self-ingest` needs a `TESTPULSE_DATABASE_URL` secret to accumulate anything.
  Without one it writes to a throwaway runner-local file and warns, which is
  honest but useless. It becomes real when Phase 6 deploys a database.
- No Playwright sharding. The suite is 21 tests and does not need it; the Cal.com
  project will.
- The Docker images are built in CI and never pushed. Also Phase 6.

### The accessibility bugs I had previously talked myself out of

The E2E suite runs axe against ten pages — five views, two themes — each loaded
fresh with the theme set before first paint. It failed on the first honest run,
and it was right.

**Badges failed contrast in both themes.** A badge draws its background by mixing
its status colour into the card at 14-16%, and I used that same status colour for
the text. So the text was a hue sitting on a washed-out version of itself:
measured 3.73:1 for the danger badge on dark, 4.01:1 for the warning badge on
light. Badge foregrounds are now their own tokens, lifted two steps from the
status colour they are tinted with.

**The light-mode accent failed too.** `#0284c7` measured 3.91:1 on the page
background and 4.09:1 on a card, and it is used for 12px link text. Darkened to
`#0369a1`.

I had previously convinced myself the light-mode failures were a measurement
artifact (see the Phase 4 note, now corrected). They were not. What made the
difference was the instrument: a scan of a page whose theme I had flipped from
the console is not the same as a scan of a page that loaded in that theme. The
first attempt gave me a reading that looked impossible, and I used "impossible"
as a reason to dismiss the whole finding rather than to fix how I was measuring.

### Two of my own tests were flaky by construction

The same run failed two E2E tests, and neither was an app bug.

**A strict-mode violation.** `getByText("Pass rate")` also matches the heading
"Pass rate over time". Needed `exact: true`.

**A locator that the action invalidates.** I located the expand toggle by
`name: /show run history/i`, clicked it, then asserted on the same locator. The
button's accessible name deliberately changes from "Show" to "Hide" when
expanded, so after the click the locator matched nothing. It now locates by
`aria-controls`, which is stable across the state change.

That second one is the more useful mistake. A locator that depends on text the
action itself changes is a test that passes when it is fast and fails when it is
slow, which is precisely the class of flake this whole project exists to detect.

---

## Phase 6 — Deployment

### Reads open, writes authenticated

Not "the API needs auth". The two halves have opposite requirements.

The read side is a dashboard whose entire purpose is being linkable — the point
of the tool is sending someone the flaky test you want them to look at, and a
login in front of that defeats it. The write side accepts a file, unpacks an
archive and stores the result, and anyone who can reach it can pollute every
metric the dashboard computes.

So `POST /api/ingest` takes a bearer token and everything else stays open.

Not OAuth, not JWT, no user model. The client is a CI job with a secret in its
environment and exactly one permitted action. A token compared in constant time
is correctly sized for that; anything more is machinery with no requirement
behind it.

Keys are a **list**, because rotating a single key means a window where either
the old CI jobs or the new ones are broken. Add the new one, migrate, drop the
old.

### Open by default, but it cannot stay open in production

No keys configured means no auth. That is deliberate — the CLI and a local
`docker compose up` should not need a secret to try the thing out.

The obvious risk is that "open" quietly becomes the state of a deployed instance.
So `create_app()` **raises** when `TESTPULSE_ENV=production` and no keys are set.
A guard, not a warning: a warning in a deployed service's startup log is a
warning nobody reads, and the failure it protects against is anyone on the
internet writing to the database every number is computed from.

### The action still writes to the database directly, and that is wrong

I noticed this while wiring the auth up. I had added an `ingest-key` input to the
action, and then realised it did nothing — the action shells out to the CLI, and
the CLI writes to the database. It never touches the API.

An input that looks like it secures something and does not is worse than no
input, so I removed it rather than leaving it there looking reassuring.

The real problem it exposed: this action needs a **database URL** in CI. Handing
every CI job a production database credential is strictly worse than handing it a
scoped API key that can only append test results. The right shape is the action
POSTing to `/api/ingest` with a bearer token. The endpoint exists and is now
authenticated; the CLI cannot target it yet.

That is recorded as the top item in "what I would build next" rather than quietly
left. It is the most security-relevant known gap in the project.

### Fly for the API, Vercel for the dashboard

Fly because scale-to-zero with a managed Postgres is free-tier viable, and a
portfolio demo that costs money every month is a portfolio demo that gets
switched off in six weeks. `min_machines_running = 0` and the first request after
a suspend pays a cold start, which is the correct trade for something nobody is
paying for.

Migrations run as a `release_command`, before new machines take traffic — the
same reasoning as the compose setup. An app that migrates in its entrypoint
cannot be scaled and two instances racing to migrate is a bug waiting.

Vercel rewrites `/api` to the Fly app, so the dashboard talks to a same-origin
path. That is now true in three environments — Vite dev proxy, nginx in Docker,
Vercel rewrite — which means one set of paths and **no CORS configuration
anywhere**. Worth the small duplication.

### Nothing is actually deployed, and the README says so

There is no live demo link on the README, because there is no live instance. I
would rather ship a README with no link than one pointing at a 404 or, worse, a
link that works today and rots.

Deploying needs accounts and credentials that are mine to create, so it is a step
I take rather than one that gets automated in a commit.

### The README is a case study, not documentation

It opens with the problem, not the feature list. It explains what each flake
threshold excludes and why, rather than listing the thresholds. It shows the PR
comment rather than describing it. And it has a section on what I got wrong.

That last part is deliberate. Everything in this repo works, which is not
interesting on its own — anyone can produce a green build. What is worth reading
is which decisions were close, what they cost, and where real data contradicted
what I had assumed. A README that only lists capabilities gives a reader no way
to tell whether the person who wrote it understood any of it.

### Left open after Phase 6

- Actually deploying. Configs written, nothing running.
- The action's database-direct write path (see above). Highest-priority gap.
- No rate limiting on ingest. A valid key can post as fast as it likes.
- Ingest keys have no scope: one key can write to any suite. Fine for one team,
  wrong the moment two share an instance.
- No `testpulse-core` release on PyPI, so the action installs from git.
