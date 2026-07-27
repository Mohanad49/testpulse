# Decisions

> **This is a draft, not the finished document.**
>
> The rule for this repo is that these entries get written in my own words, and
> that if I can't write one, I don't understand the phase yet. This draft exists
> so I'm editing rather than staring at a blank file — but reviewing prose I
> didn't write feels like understanding and isn't, which is the exact failure the
> rule was meant to prevent.
>
> Each entry ends with a **Defend this** question that is deliberately left
> unanswered. Answering those in my own words, and rewriting the entries in my
> own voice, is what makes this document real. Until then, treat every claim
> below as "the code does this", not as "I can argue for this".

---

## Phase 1 — Ingestion and storage

### 1. Parsers never touch the database

Parsers are pure functions of the filesystem: a path and some CI metadata go in,
plain dataclasses come out. A separate repository module maps those dataclasses
onto SQLAlchemy entities.

**Alternative rejected:** parsers constructing ORM objects directly. It removes
one mapping function, and in exchange every parser test needs a live session and
a schema, and the storage shape can no longer change without touching parser
code.

**Cost accepted:** two model layers that have to be kept in sync by hand.

**Defend this:** at what size does the duplicated model layer stop being worth
it, and what would the first symptom of that be?

---

### 2. `test_id` is `file_path::class::name`, with source positions stripped

This is the key that joins one test's results across runs, so everything in
Phase 2 depends on it being stable.

Playwright embeds a source position in its identifiers
(`recruitment/recruitment.spec.ts:65:7`). Inserting a line anywhere *above* a
test changes that number, which would mint a new `test_id` and reset the test's
history on a run where nothing about the test changed. So trailing `:line:col`
is stripped.

**Alternatives rejected:**
- *Report-supplied identifiers* (Allure's `historyId`): computed differently by
  each framework adapter, so they cannot join a suite's history across a change
  of tooling — which is the exact scenario this project exists to serve.
- *Fuzzy name matching*: silently merges genuinely distinct tests. A wrong merge
  corrupts the metrics; a reset history only empties them. Empty is recoverable.

**Known limitation, not solved:** renaming a test, moving its file, or renaming
its class strands the old history. The raw components are stored in their own
columns so a future version can re-key existing rows without re-ingesting.

**Defend this:** if a team renames 200 tests in one refactor, what does the
flakiness leaderboard show the next morning, and is that the right behaviour?

---

### 3. Four normalised statuses, with the original always preserved

Everything maps to `passed` / `failed` / `skipped` / `error`. The source
vocabulary is kept verbatim in `raw_status`.

The line drawn: `failed` means an assertion did not hold; `error` means the test
never reached a verdict. Teams respond differently to those two, so they stay
apart. Allure's `broken` and JUnit's `<error>` are the same idea and map the
same way.

**Judgement calls, not facts:**
- Playwright `timedOut` → `failed`. A timeout is usually a slow or stuck product,
  which is a product failure.
- Playwright `interrupted` → `error`. The run was killed; the test never got a
  verdict.
- pytest `xpassed` → `error`. The test did not fail, but a stale `xfail` marker
  is a real problem, and recording it as a pass hides it.

**Defend this:** `timedOut → failed` is the one most likely to be challenged.
What breaks in the metrics if it maps to `error` instead?

---

### 4. `retry_count` is nullable, and `NULL` ≠ `0`

`NULL` means the format cannot report retries. `0` means it can and there were
none.

This matters because Phase 2's high-precision flake strategy looks for one commit
producing two different outcomes. Defaulting to `0` would let it conclude "this
test was not retried" from a format that structurally cannot say — so the
strategy would look like it was running on JUnit data while quietly finding
nothing.

Currently: Playwright reports retries; JUnit, pytest-json-report and Allure do
not. Allure *does* record retries, as separate result files sharing a
`historyId`, but recovering that needs cross-file correlation and is Phase 2 work.

**Defend this:** Playwright only retries a failing test, so `retry_count > 0`
plus a passing final status proves same-run disagreement. Why is no extra field
needed to carry that into Phase 2?

---

### 5. Ingest is idempotent, keyed on (suite, commit, environment, started_at)

Re-ingesting one artifact must not double every metric computed over it. A
duplicate raises rather than being silently skipped, because an ingest that
quietly does nothing is indistinguishable at the call site from one that worked.

Including `started_at` in the key means a **genuine re-run of the same commit is
not a duplicate** — it gets stored as its own run. That is deliberate: Phase 2's
same-commit flake strategy is built on exactly those repeated runs and would
have nothing to read if they were collapsed.

**Alternative rejected:** an `allow_duplicate` flag that stored a second copy. It
was in the first version of this code and could not work — the unique constraint
forbids precisely what it promised. The operation actually wanted when
re-ingesting a corrected artifact is `--replace`, which deletes the stored run
first.

**Known weakness:** a report with no timestamps falls back to ingest time, so
every ingest of it gets a distinct `started_at` and this check never fires.
Formats without timestamps have no duplicate protection.

**Defend this:** two CI jobs ingest the same artifact at the same moment and both
pass the pre-check. What actually stops the double write, and why is the Python
check still worth having?

---

### 6. Malformed input is rejected, never partially salvaged

A parse error raises instead of returning the results it managed to read.

The sharpest case is truncated JUnit XML. JUnit encodes "passed" as the *absence*
of a `<failure>` child, so a file cut off mid-document parses into a shorter
suite in which everything that survived looks green. A partial ingest there turns
a broken upload into a clean run. The same logic makes an empty Allure directory
an error: "the suite produced nothing" and "the results were never written" look
identical and need different responses.

**Defend this:** the trade is that one corrupt file in a 500-file Allure
directory loses the whole run. When would you want the opposite, and what would
you need to add to make partial ingest safe?

---

### 7. An Allure results directory is not a run boundary

Found by ingesting a real directory rather than by reasoning about it. The
`allure-results` folder in the orangehrm-playwright repo held 124 result files
but only **18 distinct tests** — roughly nine accumulated runs — and the same
test appeared as passed, failed, skipped and error inside what the parser was
calling one run.

Every other format here is a file written once per run, so the file *is* the
boundary. Allure has none: the adapter appends one file per test and nothing
clears the directory unless CI does.

**Decision:** detect and warn, do not split. Splitting means deciding what
separates two runs — a time gap? a distinct test set? — and that decision belongs
with the metrics work, not the parser. The warning means it cannot happen
silently.

**Defend this:** this makes the live demo's seed data unreliable if it comes from
a locally accumulated directory. What has to change in the CI workflows of the
other portfolio repos before their results are safe to ingest?

---

### 8. Fixtures are real reporter output, not hand-written

Every fixture is genuine tool output — captured from existing suites where
possible, and otherwise produced by executing a throwaway suite and keeping its
reports verbatim.

A fixture I write myself encodes the same assumptions as the parser I am testing,
so it passes while the parser fails on real reports. Three findings came out of
using real files, and none of them would have appeared otherwise:

1. Allure attachments are nested inside steps. In 270 real results, **zero** had
   a top-level attachment and 47 had them nested. Reading `document["attachments"]`
   returns `[]` for every test and looks correct, because empty is plausible.
2. Allure's `fullName` grammar differs by adapter — `spec.ts:65:7` from
   Playwright, `pkg.Module.Class#test` from pytest — which is why identity comes
   from labels instead.
3. pytest-json-report's `created` is the session **end** time despite the name.
   Verified by cross-checking against a JUnit report from the same session:
   `created - duration` matched its timestamp to within microseconds. Used as a
   start time it would shift every run forward by its own duration.

**Defend this:** finding (3) was caught by comparing two reports of one session.
What else in this parser set has no independent source to check against, and how
would you get one?

---

### 9. `blue-ribbon-qa-automation` is excluded from fixtures and seed data

That repo contains employer test names, module names and failure messages. None
of it belongs in a public portfolio repository. This also constrains Phase 6: the
live demo cannot be seeded from it.

**Defend this:** nothing to defend. Don't relax this one later because the demo
looks thin.

---

## Still open going into Phase 2

- Reconstructing Allure retries via `historyId` correlation.
- Deciding what separates two runs inside one accumulated Allure directory.
- Whether `finished_at` should stay an estimate for JUnit (currently
  `started_at + summed test time`, which undercounts parallel runs).
