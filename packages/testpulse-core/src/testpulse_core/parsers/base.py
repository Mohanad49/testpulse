"""Parser interface and format registry.

A parser turns one report file (or results directory) into a
:class:`~testpulse_core.models.TestRun`. It is a pure function of the file
system: no database, no network, no configuration beyond the metadata handed to
it. That constraint is what makes adding a fifth format a self-contained change,
and it is why every parser test in this package is just "read a real file,
assert on the objects".
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from testpulse_core.models import RunMetadata, TestRun


class ParseError(Exception):
    """A report exists but could not be read as the format it claims to be.

    Raised for malformed input rather than returning a partial run. A test suite
    that half-parses is worse than one that fails loudly: the missing half looks
    identical to tests that were never run, and Phase 2 would read those absent
    results as tests that stopped executing.
    """


class UnknownFormatError(Exception):
    """The requested format name has no registered parser."""


@runtime_checkable
class Parser(Protocol):
    """Structural interface every format parser satisfies."""

    format_name: str

    def parse(self, path: Path, meta: RunMetadata) -> TestRun:
        """Read ``path`` and return the normalised run.

        ``path`` is a file for single-file formats and a directory for Allure.
        Raises :class:`ParseError` if the content is unreadable or malformed.
        """
        ...


_REGISTRY: dict[str, Parser] = {}


def register(parser: Parser) -> Parser:
    """Add a parser to the registry, keyed by its ``format_name``."""
    if parser.format_name in _REGISTRY:
        raise ValueError(f"Parser already registered for format {parser.format_name!r}")
    _REGISTRY[parser.format_name] = parser
    return parser


def get_parser(format_name: str) -> Parser:
    """Look up a parser by format name, e.g. ``"junit"``."""
    try:
        return _REGISTRY[format_name]
    except KeyError:
        known = ", ".join(sorted(_REGISTRY)) or "none"
        raise UnknownFormatError(
            f"No parser registered for format {format_name!r}. Known formats: {known}."
        ) from None


def available_formats() -> list[str]:
    """Every registered format name, sorted. Used by the CLI to build ``--format``."""
    return sorted(_REGISTRY)
