"""Quarantine tests.

The interesting behaviour is all about time and about what the export formats
can and cannot express, so those get the most attention.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from testpulse_core import quarantine
from testpulse_core.storage.db import create_db_engine, session_scope
from testpulse_core.storage.schema import Base, QuarantineRow

NOW = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)


@pytest.fixture
def engine(tmp_path):
    engine = create_db_engine(f"sqlite:///{tmp_path / 'q.db'}")
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


def test_adding_a_test_records_the_decision(engine):
    with session_scope(engine) as session:
        entry = quarantine.add(
            session,
            "admin-e2e",
            "tests/a.py::Cls::test_one",
            expires_after_days=14,
            reason="fails on slow CI runners",
            quarantined_by="mohanad",
            now=NOW,
        )
    assert entry.days_remaining == 14
    assert entry.is_expired is False
    assert entry.reason == "fails on slow CI runners"
    assert entry.quarantined_by == "mohanad"


def test_expiry_is_computed_from_the_quarantine_date(engine):
    with session_scope(engine) as session:
        quarantine.add(session, "s", "t", expires_after_days=14, now=NOW)

    with session_scope(engine) as session:
        entries = quarantine.list_entries(session, "s", now=NOW + timedelta(days=10))
    assert entries[0].days_remaining == 4
    assert entries[0].is_expired is False


def test_an_overdue_entry_reports_how_far_overdue(engine):
    # Signed rather than clamped at zero, because "expired" is much less
    # motivating than "expired 47 days ago".
    with session_scope(engine) as session:
        quarantine.add(session, "s", "t", expires_after_days=14, now=NOW)

    with session_scope(engine) as session:
        entries = quarantine.list_entries(session, "s", now=NOW + timedelta(days=61))
    assert entries[0].is_expired is True
    assert entries[0].days_remaining == -47


def test_re_quarantining_resets_the_clock_instead_of_stacking(engine):
    with session_scope(engine) as session:
        quarantine.add(session, "s", "t", expires_after_days=14, reason="first", now=NOW)
    with session_scope(engine) as session:
        quarantine.add(
            session,
            "s",
            "t",
            expires_after_days=7,
            reason="still broken",
            now=NOW + timedelta(days=20),
        )

    with session_scope(engine) as session:
        rows = session.query(QuarantineRow).all()
        assert len(rows) == 1
        entries = quarantine.list_entries(session, "s", now=NOW + timedelta(days=20))
    assert entries[0].reason == "still broken"
    assert entries[0].days_remaining == 7


def test_debt_lists_only_the_overdue_entries(engine):
    with session_scope(engine) as session:
        quarantine.add(session, "s", "old", expires_after_days=1, now=NOW)
        quarantine.add(session, "s", "fresh", expires_after_days=90, now=NOW)

    with session_scope(engine) as session:
        entries = quarantine.list_entries(session, "s", now=NOW + timedelta(days=30))
    overdue = quarantine.debt(entries)
    assert [e.test_id for e in overdue] == ["old"]


def test_entries_are_sorted_most_overdue_first(engine):
    # The debt should be the first thing anyone reading the list sees.
    with session_scope(engine) as session:
        quarantine.add(session, "s", "fresh", expires_after_days=90, now=NOW)
        quarantine.add(session, "s", "old", expires_after_days=1, now=NOW)
        quarantine.add(session, "s", "middling", expires_after_days=30, now=NOW)

    with session_scope(engine) as session:
        entries = quarantine.list_entries(session, "s", now=NOW + timedelta(days=31))
    assert [e.test_id for e in entries] == ["old", "middling", "fresh"]


def test_removing_a_test_that_is_not_quarantined_is_an_error(engine):
    with pytest.raises(quarantine.QuarantineError, match="not quarantined"), session_scope(
        engine
    ) as session:
        quarantine.remove(session, "s", "never-added")


def test_removing_works(engine):
    with session_scope(engine) as session:
        quarantine.add(session, "s", "t", expires_after_days=14, now=NOW)
    with session_scope(engine) as session:
        quarantine.remove(session, "s", "t")
    with session_scope(engine) as session:
        assert quarantine.list_entries(session, "s", now=NOW) == []


def test_quarantine_is_scoped_per_suite(engine):
    with session_scope(engine) as session:
        quarantine.add(session, "suite-a", "t", expires_after_days=14, now=NOW)
    with session_scope(engine) as session:
        assert quarantine.list_entries(session, "suite-b", now=NOW) == []


# --------------------------------------------------------------------------
# export formats
# --------------------------------------------------------------------------


def entries_for(*test_ids: str) -> list[quarantine.QuarantineEntry]:
    return [
        quarantine.QuarantineEntry(
            suite_name="s",
            test_id=test_id,
            quarantined_at=NOW,
            expires_after_days=14,
            reason=None,
            quarantined_by=None,
            days_remaining=14,
        )
        for test_id in test_ids
    ]


def test_json_export_includes_expiry_so_a_consumer_can_reason_about_age():
    payload = json.loads(quarantine.as_json(entries_for("tests/a.py::Cls::test_one")))
    assert payload[0]["test_id"] == "tests/a.py::Cls::test_one"
    assert payload[0]["expired"] is False
    assert payload[0]["expires_at"].startswith("2026-07-15")


def test_pytest_export_emits_usable_deselect_arguments():
    output = quarantine.as_pytest_deselect(entries_for("tests/a.py::Cls::test_one"))
    assert output == "--deselect tests/a.py::Cls::test_one"


def test_pytest_export_refuses_ids_with_no_file_path():
    # JUnit rarely gives a file, leaving ids like "::Cls::test". pytest cannot
    # deselect those, and emitting them anyway would fail silently at collection.
    output = quarantine.as_pytest_deselect(entries_for("::SomeClass::test_one"))
    assert output.startswith("# skipped:")
    assert "--deselect" not in output


def test_playwright_export_escapes_regex_metacharacters():
    # Parametrised test names are full of brackets, and an unescaped "[" makes
    # the whole grep-invert pattern either wrong or invalid.
    output = quarantine.as_playwright_grep(
        entries_for("spec.ts::Suite::books a slot [Africa/Cairo] (retry)")
    )
    assert r"\[Africa/Cairo\]" in output
    assert r"\(retry\)" in output


def test_playwright_export_joins_titles_with_alternation():
    output = quarantine.as_playwright_grep(
        entries_for("a.spec.ts::S::alpha", "b.spec.ts::S::beta")
    )
    assert output == "alpha|beta"


def test_playwright_export_of_nothing_is_empty_not_a_match_everything_regex():
    # An empty alternation would match every test and skip the entire suite.
    assert quarantine.as_playwright_grep([]) == ""
