"""Static export of everything the dashboard reads.

Why this exists: a public demo needs to be free, instant, and still be there in a
year. Every free application host either sleeps (a ~50 second cold start on the
one visit that matters) or withdraws its free tier eventually. A static file on a
CDN does neither.

So CI writes the real data to Postgres, and then dumps the exact shapes the API
returns into JSON. The dashboard reads those files instead of calling an API. The
API still exists, is still tested, and is still what a self-hosted install runs -
this is a second consumer of the same query layer, not a replacement for it.

One file per suite rather than one per endpoint. Two reasons: the whole dashboard
for a suite is a single request instead of five, and per-test detail would
otherwise need a filename derived from a ``test_id``, which contains slashes,
spaces and parentheses. Encoding those into safe filenames is a problem with no
good answer and this design does not have it.
"""

from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from testpulse_core import quarantine as quarantine_service
from testpulse_core.config import Settings
from testpulse_core.storage import queries

# The dashboard renders a bounded strip of cells and a bounded table. Exporting
# unbounded history would grow the payload without changing a single pixel.
MAX_TIMELINE_POINTS = 120
MAX_RECENT_RUNS = 40


def _json_safe(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if is_dataclass(value) and not isinstance(value, type):
        return {k: _json_safe(v) for k, v in asdict(value).items()}
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_json_safe(v) for v in value]
    return value


def export_suite(session: Session, suite: str, settings: Settings) -> dict[str, Any]:
    """Build one suite's complete payload, matching the API's field names exactly.

    Field names are kept identical to the API responses on purpose: the frontend
    then needs one adapter that chooses where bytes come from, rather than two
    parallel sets of types that can drift apart silently.
    """
    health = queries.suite_health(session, suite, settings.flake, settings.newly_failing)
    metrics = queries.suite_metrics(session, suite, settings.flake, settings.newly_failing)
    clusters = queries.suite_failure_clusters(session, suite, settings.flake)
    entries = quarantine_service.list_entries(session, suite)
    quarantined = {entry.test_id for entry in entries}

    def metric_payload(metric: Any) -> dict[str, Any]:
        payload: dict[str, Any] = dict(_json_safe(metric))
        payload["flake_evidence"] = list(metric.flake_evidence)
        payload["is_quarantined"] = metric.test_id in quarantined
        return payload

    details: dict[str, Any] = {}
    for metric in metrics:
        history = queries.test_history(
            session, suite, metric.test_id, limit=MAX_TIMELINE_POINTS
        )
        latest = history[-1][1] if history else None
        details[metric.test_id] = {
            "metrics": metric_payload(metric),
            "timeline": [
                {
                    "run_id": run.id,
                    "started_at": run.started_at.isoformat(),
                    "commit_sha": run.commit_sha,
                    "branch": run.branch,
                    "status": result.status,
                    "raw_status": result.raw_status,
                    "duration_ms": result.duration_ms,
                    "retry_count": result.retry_count,
                    "failure_message": result.failure_message,
                }
                for run, result in history
            ],
            "attachments": (latest.attachments or "").split("\n")
            if latest and latest.attachments
            else [],
        }

    return {
        "suite": suite,
        "generated_at": datetime.now().astimezone().isoformat(),
        "health": _json_safe(health) if health else None,
        "tests": [metric_payload(m) for m in metrics],
        "failures": [_json_safe(c) for c in clusters],
        "quarantine": {
            "suite_name": suite,
            "entries": [
                {
                    **_json_safe(entry),
                    "expires_at": entry.expires_at.isoformat(),
                    "is_expired": entry.is_expired,
                }
                for entry in entries
            ],
            "debt_count": len(quarantine_service.debt(entries)),
        },
        "details": details,
    }


def export_all(session: Session, output: Path, settings: Settings) -> list[str]:
    """Write ``index.json`` plus one file per suite. Returns the suites written."""
    output.mkdir(parents=True, exist_ok=True)
    suites = queries.list_suites(session)

    for suite in suites:
        payload = export_suite(session, suite, settings)
        # Suite names are user-supplied and can contain anything, so the filename
        # is an index rather than the name itself.
        (output / f"suite-{suites.index(suite)}.json").write_text(
            json.dumps(payload, separators=(",", ":"))
        )

    (output / "index.json").write_text(
        json.dumps(
            {
                "generated_at": datetime.now().astimezone().isoformat(),
                "suites": [
                    {"name": suite, "file": f"suite-{index}.json"}
                    for index, suite in enumerate(suites)
                ],
            },
            indent=2,
        )
    )
    return suites
