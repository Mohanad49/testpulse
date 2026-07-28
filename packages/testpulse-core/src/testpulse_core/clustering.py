"""Grouping failure messages that share a root cause.

Forty failures caused by one broken selector should read as one problem, not
forty. The whole value is turning a wall of near-identical text into a short
list, so the interesting question is not "can we group these" but "what happens
when we group two things that are not actually the same".

Approach: normalise each message into a template by replacing the parts that
vary between occurrences (numbers, timings, ids, paths, quoted values), then
group messages whose templates are byte-identical.

Why exact-match on templates rather than fuzzy similarity. A false merge hides a
real second bug inside a cluster labelled as something else, and nobody goes
looking for it because the count looks explained. A false split shows two rows
that a human instantly recognises as the same thing. The failure modes are not
symmetric, so the algorithm is deliberately biased toward splitting. This is the
same reasoning as the test_id decision: wrong data is more dangerous than
missing data.

Known false-merge case, since precision is the claim: two genuinely different
assertions that differ only in a number normalise to the same template.
``expected 3 items`` and ``expected 7 items`` become one cluster. That is usually
correct and occasionally is not.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

# Order matters. Longer, more specific patterns run first so a UUID is not first
# chewed up by the hex-number rule and left as unrecognisable fragments.
_NORMALISERS: tuple[tuple[re.Pattern[str], str], ...] = (
    # UUIDs, before anything touches their hex digits.
    (
        re.compile(
            r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
            re.IGNORECASE,
        ),
        "<uuid>",
    ),
    # Timings, before bare numbers, so "5000ms" does not become "<n>ms" in one
    # place and "<n> ms" in another.
    (re.compile(r"\b\d+(?:\.\d+)?\s*(?:ms|milliseconds?)\b", re.IGNORECASE), "<duration>"),
    (re.compile(r"\b\d+(?:\.\d+)?\s*(?:s|secs?|seconds?)\b", re.IGNORECASE), "<duration>"),
    # Absolute and relative file paths with a line reference.
    (re.compile(r"(?:/[\w.\-]+)+\.\w+:\d+(?::\d+)?"), "<location>"),
    (re.compile(r"[\w.\-/]+\.(?:py|ts|tsx|js|jsx|java|rb|go):\d+(?::\d+)?"), "<location>"),
    # Hex literals and long hex strings (commit shas, object addresses).
    (re.compile(r"\b0x[0-9a-f]+\b", re.IGNORECASE), "<hex>"),
    (re.compile(r"\b[0-9a-f]{7,}\b", re.IGNORECASE), "<hex>"),
    # ISO timestamps.
    (re.compile(r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}\S*"), "<timestamp>"),
    # URLs.
    (re.compile(r"https?://\S+"), "<url>"),
    # Quoted values. These are usually the specific thing under test, which is
    # exactly what varies between two occurrences of one bug.
    (re.compile(r"'[^']*'"), "'<value>'"),
    (re.compile(r'"[^"]*"'), '"<value>"'),
    # Anything numeric left over.
    (re.compile(r"\b\d+(?:\.\d+)?\b"), "<n>"),
    # Collapse whitespace last so earlier patterns can rely on it.
    (re.compile(r"\s+"), " "),
)


@dataclass(frozen=True, slots=True)
class FailureCluster:
    """A group of failures that normalise to the same template."""

    template: str
    """The normalised form. Shown as the cluster heading because it reads as the
    shape of the problem with the noise removed."""

    count: int
    representative: str
    """One real, unedited message from the cluster. The template alone is not
    enough to debug from; somebody needs to see an actual failure."""

    test_ids: list[str] = field(default_factory=list)
    """Which tests produced it. A cluster spanning many tests is a strong hint at
    shared infrastructure rather than a bug in any one test."""


def normalise(message: str) -> str:
    """Reduce a failure message to its template.

    Public because the clustering is only defensible if someone can run this on
    two messages and see exactly why they did or did not group.
    """
    text = message.strip()
    for pattern, replacement in _NORMALISERS:
        text = pattern.sub(replacement, text)
    return text.strip()


def cluster_failures(
    failures: list[tuple[str, str]],
    limit: int | None = None,
) -> list[FailureCluster]:
    """Group ``(test_id, failure_message)`` pairs by normalised template.

    Returns clusters largest first, since the biggest cluster is where fixing one
    thing removes the most red.

    Empty and whitespace-only messages are dropped rather than forming a cluster
    of their own. A failure with no message is a reporting gap, not a root cause,
    and letting it become the largest "cluster" would push real problems down the
    page.
    """
    grouped: dict[str, list[tuple[str, str]]] = {}
    for test_id, message in failures:
        if not message or not message.strip():
            continue
        grouped.setdefault(normalise(message), []).append((test_id, message))

    clusters = [
        FailureCluster(
            template=template,
            count=len(members),
            # The most common exact message, so the example shown is typical of
            # the cluster rather than whichever one happened to be first.
            representative=Counter(message for _, message in members).most_common(1)[0][0],
            test_ids=sorted({test_id for test_id, _ in members}),
        )
        for template, members in grouped.items()
    ]
    clusters.sort(key=lambda c: (-c.count, c.template))
    return clusters[:limit] if limit else clusters
