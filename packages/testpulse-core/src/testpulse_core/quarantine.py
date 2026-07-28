"""Quarantine: deciding to stop trusting a test, with a deadline attached.

Two positions this module takes, both arguable and both on purpose.

**Quarantining is a human act.** The classifier flags candidates; a person
records the decision. Auto-quarantining whatever the metrics flag means tests
stop gating merges because a number moved overnight, with nobody's name on it.

**Every entry expires.** A quarantine list without expiry becomes a graveyard.
Tests get disabled during a bad week, the incident passes, and three years later
nobody knows why 60 tests are skipped or whether the code they covered still
works. The expiry does not delete anything or re-enable anything on its own; it
just makes the list stop being silent about age. That surfaced debt is the whole
feature.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from testpulse_core.storage.schema import QuarantineRow


class QuarantineError(Exception):
    """The requested quarantine change cannot be made."""


@dataclass(frozen=True, slots=True)
class QuarantineEntry:
    """A stored quarantine decision, with its age worked out."""

    suite_name: str
    test_id: str
    quarantined_at: datetime
    expires_after_days: int
    reason: str | None
    quarantined_by: str | None
    days_remaining: int
    """Negative once expired. Kept signed rather than clamped at zero so a report
    can say how far overdue an entry is, which is the number that gets a team to
    act."""

    @property
    def is_expired(self) -> bool:
        return self.days_remaining < 0

    @property
    def expires_at(self) -> datetime:
        return self.quarantined_at + timedelta(days=self.expires_after_days)


def _to_entry(row: QuarantineRow, now: datetime) -> QuarantineEntry:
    quarantined_at = row.quarantined_at
    if quarantined_at.tzinfo is None:
        # SQLite hands datetimes back naive. They were stored as UTC.
        quarantined_at = quarantined_at.replace(tzinfo=UTC)
    expires_at = quarantined_at + timedelta(days=row.expires_after_days)
    return QuarantineEntry(
        suite_name=row.suite_name,
        test_id=row.test_id,
        quarantined_at=quarantined_at,
        expires_after_days=row.expires_after_days,
        reason=row.reason,
        quarantined_by=row.quarantined_by,
        days_remaining=(expires_at - now).days,
    )


def add(
    session: Session,
    suite_name: str,
    test_id: str,
    *,
    expires_after_days: int,
    reason: str | None = None,
    quarantined_by: str | None = None,
    now: datetime | None = None,
) -> QuarantineEntry:
    """Quarantine a test, or refresh an existing decision about it.

    Re-quarantining resets the clock rather than adding a second row. That is a
    real decision being made again ("yes, still broken, give it another two
    weeks") and it should be visible as a new date, not as a duplicate.
    """
    moment = now or datetime.now(UTC)
    existing = session.execute(
        select(QuarantineRow).where(
            QuarantineRow.suite_name == suite_name,
            QuarantineRow.test_id == test_id,
        )
    ).scalar_one_or_none()

    if existing is not None:
        existing.quarantined_at = moment
        existing.expires_after_days = expires_after_days
        existing.reason = reason
        existing.quarantined_by = quarantined_by
        session.flush()
        return _to_entry(existing, moment)

    row = QuarantineRow(
        suite_name=suite_name,
        test_id=test_id,
        quarantined_at=moment,
        expires_after_days=expires_after_days,
        reason=reason,
        quarantined_by=quarantined_by,
    )
    session.add(row)
    session.flush()
    return _to_entry(row, moment)


def remove(session: Session, suite_name: str, test_id: str) -> None:
    """Take a test out of quarantine. Raises if it was not in there."""
    row = session.execute(
        select(QuarantineRow).where(
            QuarantineRow.suite_name == suite_name,
            QuarantineRow.test_id == test_id,
        )
    ).scalar_one_or_none()
    if row is None:
        raise QuarantineError(f"{test_id!r} is not quarantined in suite {suite_name!r}")
    session.delete(row)


def list_entries(
    session: Session,
    suite_name: str,
    now: datetime | None = None,
) -> list[QuarantineEntry]:
    """Every entry for a suite, most overdue first.

    Sorted by days remaining ascending so expired entries are at the top. The
    debt should be the first thing anyone reading this list sees.
    """
    moment = now or datetime.now(UTC)
    rows = session.execute(
        select(QuarantineRow).where(QuarantineRow.suite_name == suite_name)
    ).scalars().all()
    entries = [_to_entry(row, moment) for row in rows]
    entries.sort(key=lambda e: e.days_remaining)
    return entries


def debt(entries: list[QuarantineEntry]) -> list[QuarantineEntry]:
    """Entries that are past their expiry. The number a team should be shown."""
    return [entry for entry in entries if entry.is_expired]


# --------------------------------------------------------------------------
# Export formats. These exist so CI can act on the list without parsing prose.
# --------------------------------------------------------------------------


def _test_name_from_id(test_id: str) -> str:
    """Recover the test's own name from a composite id.

    ids are ``file::class::name``, so the last segment is the name. Falls back to
    the whole id if it does not split, which happens for formats that gave us
    nothing to build a scoped id from.
    """
    return test_id.rsplit("::", 1)[-1] or test_id


def as_json(entries: list[QuarantineEntry]) -> str:
    """Full detail, including expiry, for anything that wants to reason about it."""
    return json.dumps(
        [
            {
                "suite": entry.suite_name,
                "test_id": entry.test_id,
                "quarantined_at": entry.quarantined_at.isoformat(),
                "expires_at": entry.expires_at.isoformat(),
                "days_remaining": entry.days_remaining,
                "expired": entry.is_expired,
                "reason": entry.reason,
                "quarantined_by": entry.quarantined_by,
            }
            for entry in entries
        ],
        indent=2,
    )


def as_pytest_deselect(entries: list[QuarantineEntry]) -> str:
    """``--deselect`` arguments, one per line.

    The brief called this format "pytest markers", and markers turn out to be the
    wrong mechanism. A marker has to be written into the source file next to the
    test, so applying one from an external list means either editing test files
    from CI or adding a conftest hook that reads the list and rewrites collected
    items. Both are more machinery than the job needs.

    ``--deselect`` takes a nodeid on the command line and does exactly this, with
    nothing to install. The tradeoff is that a deselected test does not appear in
    the report at all, whereas a marked one could be reported as skipped-because-
    quarantined. If that visibility turns out to matter, the conftest hook is the
    upgrade path.

    A ``test_id`` happens to already be in nodeid shape, but only when the source
    report gave us a file path. JUnit usually does not, which leaves ids like
    ``::SomeClass::test_name``, and pytest cannot deselect those. Those entries
    are skipped with a comment rather than emitted as arguments that would fail
    silently at collection time.
    """
    lines: list[str] = []
    for entry in entries:
        file_segment = entry.test_id.split("::", 1)[0]
        if not file_segment:
            lines.append(
                f"# skipped: {entry.test_id} has no file path, "
                "so it cannot be expressed as a pytest nodeid"
            )
            continue
        lines.append(f"--deselect {entry.test_id}")
    return "\n".join(lines)


def as_playwright_grep(entries: list[QuarantineEntry]) -> str:
    """A regex for ``--grep-invert``.

    Playwright greps on the test's title, not on a file path, so this can only
    use the name segment of the id. Titles are escaped because test names
    routinely contain regex metacharacters: parentheses, plus signs, and the
    square brackets that parametrised tests are full of.

    Known imprecision: two tests in different files with identical titles cannot
    be told apart by a title regex, so quarantining one skips both. Worth
    knowing before wiring this into a pipeline.
    """
    if not entries:
        return ""
    titles = sorted({re.escape(_test_name_from_id(entry.test_id)) for entry in entries})
    return "|".join(titles)
