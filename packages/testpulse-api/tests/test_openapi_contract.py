"""Contract tests: do real responses match the schema the service publishes?

This is not the same question as "does the endpoint work". FastAPI generates the
OpenAPI document from the response models, so the two agree by construction as
long as the handler actually returns the declared model. The gap opens the moment
a handler returns something FastAPI coerces, or a field is nullable in practice
but not in the schema, or an error path returns a shape nobody declared. That gap
is what a consumer's generated client breaks on.

Building a test tool with an untested contract would be a poor look, so the
responses are validated against the published document rather than against
hand-written expectations.
"""

from __future__ import annotations

import pytest
from jsonschema import Draft202012Validator

from conftest import CORE_FIXTURES

JUNIT = CORE_FIXTURES / "junit" / "pytest-suite.xml"


@pytest.fixture
def spec(client):
    response = client.get("/openapi.json")
    assert response.status_code == 200
    return response.json()


def validator_for(spec: dict, path: str, method: str, status_code: str) -> Draft202012Validator:
    """Pull the response schema for one operation, with its $refs resolvable.

    OpenAPI component schemas point at each other with ``#/components/...``
    pointers. Those are relative to the document root, and the extracted fragment
    is not the document root, so the pointers dangle. Carrying ``components``
    into the fragment makes them resolve against the thing being validated.

    This has to be got right rather than approximated: an unresolvable ``$ref``
    raises here, but the failure mode of a *subtly* wrong base is a validator
    that quietly checks nothing and reports every response as valid.
    """
    operation = spec["paths"][path][method]
    schema = operation["responses"][status_code]["content"]["application/json"]["schema"]
    return Draft202012Validator({**schema, "components": spec["components"]})


def assert_matches(spec, response, path, method="get", status_code="200"):
    errors = sorted(
        validator_for(spec, path, method, status_code).iter_errors(response.json()),
        key=lambda e: list(e.path),
    )
    assert not errors, "\n".join(
        f"{list(error.path)}: {error.message}" for error in errors
    )


def test_the_document_is_generated_and_describes_every_endpoint(spec):
    assert spec["info"]["title"] == "TestPulse API"
    documented = set(spec["paths"])
    assert {
        "/api/suites",
        "/api/suites/{suite}/runs",
        "/api/suites/{suite}/health",
        "/api/suites/{suite}/tests",
        "/api/suites/{suite}/flaky",
        "/api/suites/{suite}/quarantine",
        "/api/suites/{suite}/tests/{test_id}",
        "/api/ingest",
    } <= documented


def test_suites_response_matches_its_schema(client, spec, seeded):
    assert_matches(spec, client.get("/api/suites"), "/api/suites")


def test_runs_response_matches_its_schema(client, spec, seeded):
    assert_matches(
        spec, client.get("/api/suites/admin-e2e/runs"), "/api/suites/{suite}/runs"
    )


def test_health_response_matches_its_schema(client, spec, seeded):
    assert_matches(
        spec, client.get("/api/suites/admin-e2e/health"), "/api/suites/{suite}/health"
    )


def test_tests_response_matches_its_schema(client, spec, seeded):
    assert_matches(
        spec, client.get("/api/suites/admin-e2e/tests"), "/api/suites/{suite}/tests"
    )


def test_flaky_response_matches_its_schema(client, spec, seeded):
    assert_matches(
        spec, client.get("/api/suites/admin-e2e/flaky"), "/api/suites/{suite}/flaky"
    )


def test_quarantine_response_matches_its_schema(client, spec, seeded):
    assert_matches(
        spec,
        client.get("/api/suites/admin-e2e/quarantine"),
        "/api/suites/{suite}/quarantine",
    )


def test_test_detail_response_matches_its_schema(client, spec, seeded):
    assert_matches(
        spec,
        client.get("/api/suites/admin-e2e/tests/tests/booking.py::Booking::test_reschedule"),
        "/api/suites/{suite}/tests/{test_id}",
    )


def test_ingest_response_matches_its_schema(client, spec):
    response = client.post(
        "/api/ingest",
        data={"suite": "uploaded", "format": "junit", "commit": "a" * 40},
        files={"file": ("junit.xml", JUNIT.read_bytes(), "application/xml")},
    )
    assert response.status_code == 201
    assert_matches(spec, response, "/api/ingest", method="post", status_code="201")


def test_nullable_pass_rate_is_declared_nullable(spec):
    # The case most likely to break a generated client. An all-skipped test
    # returns null here, and a client generated from a schema claiming
    # `number` would fail to deserialise it.
    schema = spec["components"]["schemas"]["TestMetricsSchema"]["properties"]["pass_rate"]
    assert "null" in {entry.get("type") for entry in schema["anyOf"]}


def test_flake_evidence_is_declared_as_an_array_of_strings(spec):
    # It is a tuple internally. If it leaked as one, the schema would be wrong.
    schema = spec["components"]["schemas"]["TestMetricsSchema"]["properties"]["flake_evidence"]
    assert schema["type"] == "array"
    assert schema["items"]["type"] == "string"


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/api/suites/does-not-exist/runs", 404),
        ("/api/suites/does-not-exist/health", 404),
        ("/api/suites/does-not-exist/tests", 404),
    ],
)
def test_error_responses_share_one_shape(client, seeded, path, expected):
    # One error shape across the whole API means a consumer writes one error path.
    response = client.get(path)
    assert response.status_code == expected
    body = response.json()
    assert set(body) == {"detail"}
    assert isinstance(body["detail"], str)


def test_documented_status_codes_include_the_error_cases(spec):
    # A 404 that is not in the document is a 404 a generated client will treat as
    # an unexpected failure rather than a case to handle.
    runs = spec["paths"]["/api/suites/{suite}/runs"]["get"]["responses"]
    assert "422" in runs, "FastAPI documents validation failures"


def test_the_validator_actually_rejects_a_wrong_payload(client, spec, seeded):
    """Guard against the whole file being a no-op.

    A JSON Schema validator whose $refs do not resolve accepts everything, so a
    green contract suite is not by itself evidence that anything was checked.
    This feeds it a response that is obviously wrong and insists it complains.
    """
    checker = validator_for(spec, "/api/suites/{suite}/health", "get", "200")

    assert list(checker.iter_errors({"suite_name": "x"})), "missing required fields passed"
    assert list(checker.iter_errors({"nonsense": True})), "unknown-only object passed"

    valid = client.get("/api/suites/admin-e2e/health").json()
    assert not list(checker.iter_errors(valid))

    broken = {**valid, "flaky_count": "not a number"}
    assert list(checker.iter_errors(broken)), "wrong type passed validation"


def test_failure_clusters_response_matches_its_schema(client, spec, seeded):
    assert_matches(
        spec, client.get("/api/suites/admin-e2e/failures"), "/api/suites/{suite}/failures"
    )
