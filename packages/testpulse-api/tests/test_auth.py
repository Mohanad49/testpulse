"""Write-authentication tests.

The point of these is not that the happy path works. It is that the endpoint is
genuinely closed when it should be, and that the "open by default" behaviour
cannot silently survive into production.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from conftest import CORE_FIXTURES
from testpulse_api.main import InsecureConfigurationError, create_app

JUNIT = CORE_FIXTURES / "junit" / "pytest-suite.xml"


def form():
    return {"suite": "authed", "format": "junit", "commit": "a" * 40}


def files():
    return {"file": ("junit.xml", JUNIT.read_bytes(), "application/xml")}


def test_ingest_is_open_when_no_keys_are_configured(client, monkeypatch):
    # The CLI and a local docker-compose should not need a secret to try the
    # tool out. This is the deliberate default, protected by the startup guard.
    monkeypatch.delenv("TESTPULSE_INGEST_KEYS", raising=False)
    assert client.post("/api/ingest", data=form(), files=files()).status_code == 201


def test_a_configured_key_makes_the_endpoint_require_one(client, monkeypatch):
    monkeypatch.setenv("TESTPULSE_INGEST_KEYS", "s3cret")
    response = client.post("/api/ingest", data=form(), files=files())
    assert response.status_code == 401
    assert response.headers.get("WWW-Authenticate") == "Bearer"


def test_the_right_key_is_accepted(client, monkeypatch):
    monkeypatch.setenv("TESTPULSE_INGEST_KEYS", "s3cret")
    response = client.post(
        "/api/ingest",
        data=form(),
        files=files(),
        headers={"Authorization": "Bearer s3cret"},
    )
    assert response.status_code == 201


def test_a_wrong_key_is_403_not_401(client, monkeypatch):
    # 401 means "you did not authenticate", 403 means "you did and it was not
    # good enough". A client can retry the first and should not retry the second.
    monkeypatch.setenv("TESTPULSE_INGEST_KEYS", "s3cret")
    response = client.post(
        "/api/ingest", data=form(), files=files(), headers={"Authorization": "Bearer wrong"}
    )
    assert response.status_code == 403


def test_several_keys_are_accepted_so_rotation_is_possible(client, monkeypatch):
    # Rotating a single key means a window where either the old jobs or the new
    # ones are broken. With a list you add, migrate, then remove.
    monkeypatch.setenv("TESTPULSE_INGEST_KEYS", "old-key, new-key")
    for key in ("old-key", "new-key"):
        response = client.post(
            "/api/ingest",
            data={**form(), "suite": f"authed-{key}"},
            files=files(),
            headers={"Authorization": f"Bearer {key}"},
        )
        assert response.status_code == 201, key


def test_a_malformed_authorization_header_is_rejected(client, monkeypatch):
    monkeypatch.setenv("TESTPULSE_INGEST_KEYS", "s3cret")
    for header in ("s3cret", "Basic s3cret", "Bearer", ""):
        response = client.post(
            "/api/ingest", data=form(), files=files(), headers={"Authorization": header}
        )
        assert response.status_code in (401, 403), header


def test_reads_stay_open_when_writes_are_locked(client, monkeypatch, seeded):
    # The read side is a dashboard whose whole purpose is being linkable.
    # Putting a login in front of "here is the flaky test" defeats the tool.
    monkeypatch.setenv("TESTPULSE_INGEST_KEYS", "s3cret")
    assert client.get("/api/suites").status_code == 200
    assert client.get("/api/suites/admin-e2e/flaky").status_code == 200


def test_production_refuses_to_start_without_keys(monkeypatch):
    # A guard, not a warning. A warning in a startup log on a deployed service is
    # a warning nobody reads.
    monkeypatch.setenv("TESTPULSE_ENV", "production")
    monkeypatch.delenv("TESTPULSE_INGEST_KEYS", raising=False)
    with pytest.raises(InsecureConfigurationError, match="unauthenticated writes"):
        create_app()


def test_production_starts_once_a_key_is_set(monkeypatch):
    monkeypatch.setenv("TESTPULSE_ENV", "production")
    monkeypatch.setenv("TESTPULSE_INGEST_KEYS", "s3cret")
    with TestClient(create_app()) as client:
        assert client.get("/health").status_code == 200


def test_local_use_is_unaffected(monkeypatch):
    monkeypatch.delenv("TESTPULSE_ENV", raising=False)
    monkeypatch.delenv("TESTPULSE_INGEST_KEYS", raising=False)
    assert create_app() is not None
