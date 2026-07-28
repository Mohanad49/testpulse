"""SQLAlchemy entities.

Deliberately separate from the dataclasses in :mod:`testpulse_core.models`.
Keeping them apart costs a mapping function and buys parsers that can be tested
without a database, plus freedom to change the storage shape (indexes,
denormalised columns for Phase 2) without touching the parser contract.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """Declarative base for every TestPulse table."""


class TestRunRow(Base):
    """One ingested report."""

    __test__ = False  # "Test*" is also pytest's collection prefix; opt out.
    __tablename__ = "test_runs"
    __table_args__ = (
        # The idempotency key. A CI job that re-runs, or an engineer who ingests
        # the same artifact twice, must not double every metric downstream.
        # started_at is part of the key rather than the whole of it because two
        # environments legitimately produce two runs of one suite at one commit.
        UniqueConstraint(
            "suite_name",
            "commit_sha",
            "environment",
            "started_at",
            name="uq_test_runs_natural_key",
        ),
        Index("ix_test_runs_suite_started", "suite_name", "started_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    suite_name: Mapped[str] = mapped_column(String(255))
    commit_sha: Mapped[str | None] = mapped_column(String(64))
    branch: Mapped[str | None] = mapped_column(String(255))
    ci_run_url: Mapped[str | None] = mapped_column(Text)
    environment: Mapped[str | None] = mapped_column(String(128))
    source_format: Mapped[str] = mapped_column(String(32))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Denormalised counts. They are derivable from the child rows, and are stored
    # anyway because the suite-overview screen in Phase 4 reads them for every run
    # on the page. Recomputing them per request would make the cheapest, most
    # frequently rendered view the most expensive query in the product.
    total: Mapped[int] = mapped_column(Integer, default=0)
    passed: Mapped[int] = mapped_column(Integer, default=0)
    failed: Mapped[int] = mapped_column(Integer, default=0)
    skipped: Mapped[int] = mapped_column(Integer, default=0)
    errored: Mapped[int] = mapped_column(Integer, default=0)

    results: Mapped[list[TestResultRow]] = relationship(
        back_populates="run",
        cascade="all, delete-orphan",
    )


class TestResultRow(Base):
    """One test outcome inside one run."""

    __test__ = False
    __tablename__ = "test_results"
    __table_args__ = (
        # Phase 2 reads a single test's history across runs. Without this index
        # every metric computation is a full scan of the largest table.
        Index("ix_test_results_test_id", "test_id"),
        Index("ix_test_results_run_status", "run_id", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("test_runs.id", ondelete="CASCADE"),
    )

    test_id: Mapped[str] = mapped_column(String(1024))
    display_name: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16))
    raw_status: Mapped[str] = mapped_column(String(32))
    duration_ms: Mapped[int] = mapped_column(Integer)

    # Stored alongside the composite test_id so a future version can re-key
    # existing rows without re-ingesting every source report.
    file_path: Mapped[str | None] = mapped_column(Text)
    class_name: Mapped[str | None] = mapped_column(Text)
    test_name: Mapped[str | None] = mapped_column(Text)

    failure_message: Mapped[str | None] = mapped_column(Text)
    failure_stack: Mapped[str | None] = mapped_column(Text)
    # Nullable on purpose: NULL means the source format cannot report retries,
    # 0 means it can and there were none.
    retry_count: Mapped[int | None] = mapped_column(Integer)
    # Attachment references are stored as a newline-joined list rather than in a
    # child table. They are only ever read as a whole set for one result and are
    # never queried across results, so a join table would add cost with no
    # corresponding query to serve. Revisit if the dashboard ever filters by
    # attachment type.
    attachments: Mapped[str | None] = mapped_column(Text)

    run: Mapped[TestRunRow] = relationship(back_populates="results")


class QuarantineRow(Base):
    """A test somebody decided to stop trusting, and when that decision expires.

    Quarantine is a stored human decision, not something derived from the flake
    metrics on the fly. Auto-quarantining every test the classifier flags would
    mean tests silently stop gating merges because a number crossed a threshold
    at 3am, and nobody would be accountable for that. The classifier proposes;
    a person decides.
    """

    __tablename__ = "quarantine"
    __table_args__ = (
        # One live entry per test per suite. Re-quarantining an already
        # quarantined test should update the existing decision, not stack a
        # second one with a different expiry.
        UniqueConstraint("suite_name", "test_id", name="uq_quarantine_suite_test"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    suite_name: Mapped[str] = mapped_column(String(255))
    test_id: Mapped[str] = mapped_column(String(1024))
    quarantined_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_after_days: Mapped[int] = mapped_column(Integer)
    reason: Mapped[str | None] = mapped_column(Text)
    quarantined_by: Mapped[str | None] = mapped_column(String(255))
