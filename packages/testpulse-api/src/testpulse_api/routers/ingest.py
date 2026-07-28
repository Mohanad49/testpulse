"""Upload endpoint.

This is the only write endpoint, and the only place the service accepts a file
from outside. Allure results are a directory, so accepting them over HTTP means
accepting an archive, and unpacking an archive supplied by a caller is the single
most dangerous thing this codebase does. The guards below are not hypothetical
hardening; every one of them corresponds to a known way to break an unpacker.
"""

from __future__ import annotations

import shutil
import tempfile
import zipfile
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, File, Form, HTTPException, UploadFile, status
from testpulse_core.models import RunMetadata
from testpulse_core.parsers import ParseError, UnknownFormatError, available_formats, get_parser
from testpulse_core.storage.db import session_scope
from testpulse_core.storage.repository import DuplicateRunError, store_run

from testpulse_api.deps import EngineDep
from testpulse_api.schemas import IngestResponseSchema

router = APIRouter(prefix="/api", tags=["ingest"])

MAX_UPLOAD_BYTES = 64 * 1024 * 1024
"""64 MB. A JUnit file is kilobytes and a large Allure directory is a few
megabytes, so this is generous. It exists because without a ceiling the endpoint
will happily buffer whatever it is sent until the process dies."""

MAX_UNCOMPRESSED_BYTES = 512 * 1024 * 1024
"""Zip compresses well, and adversarially well. A few hundred kilobytes of
archive can expand to gigabytes, so the compressed size limit above does not
constrain what unpacking costs. This one does."""

MAX_ARCHIVE_ENTRIES = 20_000
"""A directory of 20,000 result files is already implausible. Millions of tiny
entries exhaust inodes and time without ever tripping a size limit."""


def _reject(detail: str, code: int = status.HTTP_400_BAD_REQUEST) -> HTTPException:
    return HTTPException(status_code=code, detail=detail)


def _safe_extract(archive: Path, destination: Path) -> None:
    """Unpack a zip, refusing anything that tries to escape the destination.

    Three separate refusals, because they are three separate attacks:

    **Path traversal (zip slip).** An entry named ``../../etc/cron.d/x`` writes
    outside the destination. ``zipfile`` does sanitise absolute paths and
    ``..`` in ``extract()``, but relying on that is relying on an implementation
    detail of one function; the check here is on the resolved path, which is the
    property actually wanted.

    **Symlinks.** A zip can carry a symlink entry pointing at ``/etc/passwd``,
    and a later entry can then write *through* it. The resolved-path check does
    not catch this because the symlink itself lands inside the destination.
    Symlink entries are dropped outright.

    **Decompression bombs.** Checked against the declared uncompressed size
    before writing anything, so a bomb costs a header read rather than a disk.
    """
    with zipfile.ZipFile(archive) as bundle:
        entries = bundle.infolist()
        if len(entries) > MAX_ARCHIVE_ENTRIES:
            raise _reject(
                f"Archive contains {len(entries)} entries, above the limit of "
                f"{MAX_ARCHIVE_ENTRIES}."
            )

        declared_total = sum(entry.file_size for entry in entries)
        if declared_total > MAX_UNCOMPRESSED_BYTES:
            raise _reject(
                f"Archive expands to {declared_total} bytes, above the limit of "
                f"{MAX_UNCOMPRESSED_BYTES}. Refusing to unpack."
            )

        resolved_destination = destination.resolve()
        for entry in entries:
            # Unix mode is in the top 16 bits of external_attr; 0xA000 is S_IFLNK.
            if (entry.external_attr >> 16) & 0xF000 == 0xA000:
                raise _reject(f"Archive contains a symlink entry ({entry.filename!r}).")

            target = (resolved_destination / entry.filename).resolve()
            if not target.is_relative_to(resolved_destination):
                raise _reject(
                    f"Archive entry {entry.filename!r} would write outside the "
                    "extraction directory."
                )

        bundle.extractall(resolved_destination)


def _locate_report(root: Path, format_name: str) -> Path:
    """Find what the parser should be pointed at inside an extracted archive.

    Allure wants the directory holding ``*-result.json``; everything else wants a
    single file. People zip a directory and get a wrapper folder about half the
    time, so this looks one level down rather than insisting on a flat archive.
    """
    if format_name == "allure":
        if any(root.glob("*-result.json")):
            return root
        for child in sorted(p for p in root.iterdir() if p.is_dir()):
            if any(child.glob("*-result.json")):
                return child
        raise _reject("No *-result.json files found in the uploaded archive.")

    candidates = sorted(p for p in root.rglob("*") if p.is_file())
    if not candidates:
        raise _reject("The uploaded archive is empty.")
    if len(candidates) > 1:
        raise _reject(
            f"Archive holds {len(candidates)} files and format {format_name!r} expects "
            "one report. Upload the report on its own, or use --format allure for a "
            "results directory."
        )
    return candidates[0]


@router.post(
    "/ingest",
    response_model=IngestResponseSchema,
    status_code=status.HTTP_201_CREATED,
)
async def ingest(
    engine: EngineDep,
    file: Annotated[UploadFile, File(description="A report file, or a .zip of one.")],
    suite: Annotated[str, Form()],
    format_name: Annotated[str, Form(alias="format")],
    commit: Annotated[str | None, Form()] = None,
    branch: Annotated[str | None, Form()] = None,
    environment: Annotated[str | None, Form(alias="env")] = None,
    ci_run_url: Annotated[str | None, Form()] = None,
    replace: Annotated[bool, Form()] = False,
) -> IngestResponseSchema:
    """Accept a report and store it.

    Mirrors the CLI's behaviour deliberately: same formats, same duplicate rules,
    same warnings. A CI job should get the same answer whether it shells out or
    posts, and any divergence between the two would show up as a bug report about
    the tool disagreeing with itself.

    No authentication. That is a real gap and not an oversight: this writes to the
    database and anyone who can reach it can write to it. It is acceptable while
    the service runs locally or behind something else that authenticates, and it
    must be closed before the instance is public.
    """
    try:
        parser = get_parser(format_name)
    except UnknownFormatError as exc:
        raise _reject(
            f"Unknown format {format_name!r}. Known formats: {', '.join(available_formats())}.",
            status.HTTP_422_UNPROCESSABLE_ENTITY,
        ) from exc

    payload = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(payload) > MAX_UPLOAD_BYTES:
        raise _reject(
            f"Upload exceeds {MAX_UPLOAD_BYTES} bytes.",
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
        )
    if not payload:
        raise _reject("Upload is empty.")

    workspace = Path(tempfile.mkdtemp(prefix="testpulse-ingest-"))
    try:
        filename = Path(file.filename or "upload").name
        landed = workspace / filename
        landed.write_bytes(payload)

        if zipfile.is_zipfile(landed):
            extracted = workspace / "extracted"
            extracted.mkdir()
            _safe_extract(landed, extracted)
            target = _locate_report(extracted, format_name)
        elif format_name == "allure":
            raise _reject(
                "Allure results are a directory. Upload them as a .zip archive."
            )
        else:
            target = landed

        meta = RunMetadata(
            suite_name=suite,
            commit_sha=commit,
            branch=branch,
            ci_run_url=ci_run_url,
            environment=environment,
        )
        try:
            run = parser.parse(target, meta)
        except ParseError as exc:
            raise _reject(f"Could not parse report: {exc}") from exc

        try:
            with session_scope(engine) as session:
                summary = store_run(session, run, replace=replace)
        except DuplicateRunError as exc:
            # 409 rather than 400: the request was well formed, it conflicts with
            # what is already stored, and a client can act on that distinction.
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

        return IngestResponseSchema(
            run_id=summary.run_id,
            suite_name=summary.suite_name,
            results_written=summary.results_written,
            replaced_id=summary.replaced_id,
            warnings=run.warnings,
        )
    finally:
        shutil.rmtree(workspace, ignore_errors=True)
