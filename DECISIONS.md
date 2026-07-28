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
