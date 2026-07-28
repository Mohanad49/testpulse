"""Upload endpoint, including the archive attacks it has to refuse.

The malicious archives here are built in the test rather than committed, because
committing a zip bomb or a path-traversal archive to a public repository is a
good way to have the repository flagged by a scanner.
"""

from __future__ import annotations

import io
import zipfile

from conftest import CORE_FIXTURES

JUNIT = CORE_FIXTURES / "junit" / "pytest-suite.xml"
ALLURE_DIR = CORE_FIXTURES / "allure" / "playwright-producer"


def form(**overrides):
    data = {"suite": "uploaded", "format": "junit", "commit": "a" * 40, "branch": "main"}
    data.update(overrides)
    return data


def zip_of(*paths, arc_prefix: str = ""):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as bundle:
        for path in paths:
            bundle.write(path, arcname=f"{arc_prefix}{path.name}")
    buffer.seek(0)
    return buffer


# --------------------------------------------------------------------------
# happy paths
# --------------------------------------------------------------------------


def test_uploads_a_bare_report_file(client):
    response = client.post(
        "/api/ingest",
        data=form(),
        files={"file": ("junit.xml", JUNIT.read_bytes(), "application/xml")},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["results_written"] == 9
    assert body["suite_name"] == "uploaded"
    assert body["replaced_id"] is None


def test_uploaded_run_is_immediately_readable(client):
    client.post(
        "/api/ingest",
        data=form(),
        files={"file": ("junit.xml", JUNIT.read_bytes(), "application/xml")},
    )
    assert client.get("/api/suites").json() == [{"name": "uploaded"}]
    assert client.get("/api/suites/uploaded/health").json()["total_tests"] == 9


def test_uploads_an_allure_directory_as_a_zip(client):
    archive = zip_of(*sorted(ALLURE_DIR.glob("*-result.json")))
    response = client.post(
        "/api/ingest",
        data=form(suite="allure-upload", format="allure"),
        files={"file": ("results.zip", archive.getvalue(), "application/zip")},
    )
    assert response.status_code == 201, response.text
    assert response.json()["results_written"] == 4


def test_a_wrapper_folder_inside_the_zip_is_tolerated(client):
    # People zip a directory and get a wrapper folder about half the time.
    archive = zip_of(*sorted(ALLURE_DIR.glob("*-result.json")), arc_prefix="allure-results/")
    response = client.post(
        "/api/ingest",
        data=form(suite="allure-upload", format="allure"),
        files={"file": ("results.zip", archive.getvalue(), "application/zip")},
    )
    assert response.status_code == 201, response.text
    assert response.json()["results_written"] == 4


def test_parser_warnings_are_returned_to_the_caller(client):
    # An accumulated Allure directory. The caller has to be told, or the run
    # level numbers they get back are meaningless and they will not know.
    results = sorted(ALLURE_DIR.glob("*-result.json"))
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        for copy_index in range(3):
            for path in results:
                bundle.writestr(f"{copy_index}-{path.name}", path.read_bytes())
    response = client.post(
        "/api/ingest",
        data=form(suite="accumulated", format="allure"),
        files={"file": ("results.zip", buffer.getvalue(), "application/zip")},
    )
    assert response.status_code == 201
    assert any("accumulator, not a run boundary" in w for w in response.json()["warnings"])


# --------------------------------------------------------------------------
# rejections
# --------------------------------------------------------------------------


def test_unknown_format_is_422(client):
    response = client.post(
        "/api/ingest",
        data=form(format="cypress"),
        files={"file": ("junit.xml", JUNIT.read_bytes(), "application/xml")},
    )
    assert response.status_code == 422
    assert "Known formats" in response.json()["detail"]


def test_unparseable_report_is_400(client):
    truncated = (CORE_FIXTURES / "malformed" / "truncated.xml").read_bytes()
    response = client.post(
        "/api/ingest",
        data=form(),
        files={"file": ("junit.xml", truncated, "application/xml")},
    )
    assert response.status_code == 400
    assert "Could not parse report" in response.json()["detail"]


def test_reuploading_the_same_report_is_409_not_400(client):
    # The request was well formed and conflicts with stored state. A client can
    # act on that distinction; it cannot act on a generic 400.
    files = {"file": ("junit.xml", JUNIT.read_bytes(), "application/xml")}
    assert client.post("/api/ingest", data=form(), files=files).status_code == 201
    second = client.post(
        "/api/ingest",
        data=form(),
        files={"file": ("junit.xml", JUNIT.read_bytes(), "application/xml")},
    )
    assert second.status_code == 409
    assert "replace" in second.json()["detail"]


def test_replace_flag_resolves_the_conflict(client):
    client.post(
        "/api/ingest",
        data=form(),
        files={"file": ("junit.xml", JUNIT.read_bytes(), "application/xml")},
    )
    response = client.post(
        "/api/ingest",
        data=form(replace="true"),
        files={"file": ("junit.xml", JUNIT.read_bytes(), "application/xml")},
    )
    assert response.status_code == 201, response.text
    assert response.json()["replaced_id"] == 1


def test_empty_upload_is_rejected(client):
    response = client.post(
        "/api/ingest", data=form(), files={"file": ("junit.xml", b"", "application/xml")}
    )
    assert response.status_code == 400
    assert "empty" in response.json()["detail"].lower()


def test_allure_without_an_archive_explains_what_to_do(client):
    response = client.post(
        "/api/ingest",
        data=form(format="allure"),
        files={"file": ("result.json", b'{"name": "x"}', "application/json")},
    )
    assert response.status_code == 400
    assert ".zip" in response.json()["detail"]


def test_zip_with_several_files_for_a_single_file_format_is_rejected(client):
    archive = zip_of(JUNIT, CORE_FIXTURES / "junit" / "newman-restful-booker.xml")
    response = client.post(
        "/api/ingest",
        data=form(),
        files={"file": ("reports.zip", archive.getvalue(), "application/zip")},
    )
    assert response.status_code == 400
    assert "expects" in response.json()["detail"]


# --------------------------------------------------------------------------
# archive attacks
# --------------------------------------------------------------------------


def test_path_traversal_entry_is_refused(client):
    # Zip slip. An entry named ../../ escapes the extraction directory and
    # writes wherever the process can write.
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        bundle.writestr("../../../../tmp/testpulse-escaped.xml", "<testsuites/>")
    response = client.post(
        "/api/ingest",
        data=form(),
        files={"file": ("evil.zip", buffer.getvalue(), "application/zip")},
    )
    assert response.status_code == 400
    assert "outside the extraction directory" in response.json()["detail"]


def test_symlink_entry_is_refused(client):
    # A symlink lands inside the destination, so the resolved-path check does not
    # catch it, and a later entry can then write through it to anywhere.
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        info = zipfile.ZipInfo("link.xml")
        info.create_system = 3  # unix
        info.external_attr = (0o120777 << 16)  # S_IFLNK | 0777
        bundle.writestr(info, "/etc/passwd")
    response = client.post(
        "/api/ingest",
        data=form(),
        files={"file": ("evil.zip", buffer.getvalue(), "application/zip")},
    )
    assert response.status_code == 400
    assert "symlink" in response.json()["detail"]


def test_declared_expansion_over_the_limit_is_refused_before_unpacking(client):
    # A bomb costs a header read rather than a disk. This archive is a few KB
    # compressed and claims to expand past the limit.
    from testpulse_api.routers.ingest import MAX_UNCOMPRESSED_BYTES

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr("huge.xml", b"\0" * (MAX_UNCOMPRESSED_BYTES + 1))
    response = client.post(
        "/api/ingest",
        data=form(),
        files={"file": ("bomb.zip", buffer.getvalue(), "application/zip")},
    )
    assert response.status_code == 400
    assert "Refusing to unpack" in response.json()["detail"]


def test_too_many_entries_is_refused(client):
    # Millions of tiny entries exhaust inodes and time without ever tripping a
    # size limit, so entry count is its own gate.
    from testpulse_api.routers.ingest import MAX_ARCHIVE_ENTRIES

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        for index in range(MAX_ARCHIVE_ENTRIES + 1):
            bundle.writestr(f"f{index}.json", b"{}")
    response = client.post(
        "/api/ingest",
        data=form(format="allure"),
        files={"file": ("many.zip", buffer.getvalue(), "application/zip")},
    )
    assert response.status_code == 400
    assert "above the limit" in response.json()["detail"]


def test_a_refused_archive_stores_nothing(client):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        bundle.writestr("../escape.xml", "<testsuites/>")
    client.post(
        "/api/ingest",
        data=form(),
        files={"file": ("evil.zip", buffer.getvalue(), "application/zip")},
    )
    assert client.get("/api/suites").json() == []
