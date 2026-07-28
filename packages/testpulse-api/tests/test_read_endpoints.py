"""Read endpoint behaviour."""

from __future__ import annotations


def test_liveness_does_not_need_a_database(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_suites_lists_what_is_stored(client, seeded):
    response = client.get("/api/suites")
    assert response.status_code == 200
    assert response.json() == [{"name": "admin-e2e"}]


def test_runs_come_back_newest_first(client, seeded):
    runs = client.get("/api/suites/admin-e2e/runs").json()
    assert len(runs) == 8
    timestamps = [run["started_at"] for run in runs]
    assert timestamps == sorted(timestamps, reverse=True)


def test_runs_limit_is_capped(client, seeded):
    # An uncapped limit lets a caller ask the server to assemble an arbitrarily
    # large response.
    assert client.get("/api/suites/admin-e2e/runs?limit=9999").status_code == 422


def test_runs_for_an_unknown_suite_is_404(client, seeded):
    response = client.get("/api/suites/nope/runs")
    assert response.status_code == 404
    assert "No runs stored" in response.json()["detail"]


def test_health_aggregates_the_window(client, seeded):
    health = client.get("/api/suites/admin-e2e/health").json()
    assert health["suite_name"] == "admin-e2e"
    assert health["runs_in_window"] == 8
    assert health["total_tests"] == 3
    assert health["flaky_count"] == 1
    assert health["newly_failing_count"] == 1
    assert 0.0 < health["pass_rate"] < 1.0
    assert len(health["recent_runs"]) == 8


def test_health_pass_rate_weights_by_result_not_by_run(client, seeded):
    # 24 scored results across 8 runs: test_create passed 8, test_reschedule
    # passed 4, test_cancel passed 6. 18/24.
    health = client.get("/api/suites/admin-e2e/health").json()
    assert health["pass_rate"] == 18 / 24


def test_health_for_an_unknown_suite_is_404(client, seeded):
    assert client.get("/api/suites/nope/health").status_code == 404


def test_tests_endpoint_paginates_and_reports_the_total(client, seeded):
    page = client.get("/api/suites/admin-e2e/tests?limit=2&offset=0").json()
    assert page["total"] == 3
    assert len(page["items"]) == 2
    assert page["limit"] == 2
    assert page["offset"] == 0

    second = client.get("/api/suites/admin-e2e/tests?limit=2&offset=2").json()
    assert len(second["items"]) == 1


def test_tests_default_sort_is_flakiest_first(client, seeded):
    items = client.get("/api/suites/admin-e2e/tests").json()["items"]
    assert items[0]["display_name"] == "test_reschedule"
    assert items[0]["is_flaky"] is True


def test_tests_can_sort_by_any_allowed_metric(client, seeded):
    items = client.get(
        "/api/suites/admin-e2e/tests?sort_by=display_name&order=asc"
    ).json()["items"]
    assert [item["display_name"] for item in items] == [
        "test_cancel",
        "test_create",
        "test_reschedule",
    ]


def test_sorting_by_an_unknown_field_is_rejected(client, seeded):
    # An allowlist rather than getattr on user input: otherwise a request can
    # sort by any attribute that happens to exist, or crash the endpoint with a
    # typo.
    response = client.get("/api/suites/admin-e2e/tests?sort_by=__class__")
    assert response.status_code == 422
    assert "Cannot sort by" in response.json()["detail"]


def test_flaky_endpoint_returns_only_flaky_tests_with_evidence(client, seeded):
    flaky = client.get("/api/suites/admin-e2e/flaky").json()
    assert len(flaky) == 1
    assert flaky[0]["display_name"] == "test_reschedule"
    assert flaky[0]["flake_evidence"] == ["rolling-flip"]


def test_newly_failing_is_reported_separately_from_flaky(client, seeded):
    items = client.get("/api/suites/admin-e2e/tests").json()["items"]
    by_name = {item["display_name"]: item for item in items}
    # A clean history then a failure streak. Not flaky; a regression.
    assert by_name["test_cancel"]["is_newly_failing"] is True
    assert by_name["test_cancel"]["is_flaky"] is False
    # And the reverse.
    assert by_name["test_reschedule"]["is_flaky"] is True
    assert by_name["test_reschedule"]["is_newly_failing"] is False


def test_test_detail_returns_the_full_timeline(client, seeded):
    response = client.get(
        "/api/suites/admin-e2e/tests/tests/booking.py::Booking::test_reschedule"
    )
    assert response.status_code == 200, response.text
    detail = response.json()
    assert detail["metrics"]["display_name"] == "test_reschedule"
    assert len(detail["timeline"]) == 8
    statuses = [point["status"] for point in detail["timeline"]]
    assert statuses == ["passed", "failed"] * 4


def test_timeline_carries_the_commit_for_each_cell(client, seeded):
    # A strip of coloured cells is unreadable without knowing which commit each
    # one belongs to.
    detail = client.get(
        "/api/suites/admin-e2e/tests/tests/booking.py::Booking::test_reschedule"
    ).json()
    assert detail["timeline"][0]["commit_sha"] == "sha0000"
    assert all(point["started_at"] for point in detail["timeline"])


def test_test_id_containing_slashes_survives_routing(client, seeded):
    # The whole reason this route uses a :path converter. test_ids from Playwright
    # look like "recruitment/recruitment.spec.ts::Suite::name".
    response = client.get(
        "/api/suites/admin-e2e/tests/tests/booking.py::Booking::test_create"
    )
    assert response.status_code == 200
    assert response.json()["metrics"]["test_id"] == "tests/booking.py::Booking::test_create"


def test_unknown_test_is_404(client, seeded):
    response = client.get("/api/suites/admin-e2e/tests/does/not::Exist::at_all")
    assert response.status_code == 404


def test_detail_reports_attachments_from_the_latest_appearance(client, seeded):
    detail = client.get(
        "/api/suites/admin-e2e/tests/tests/booking.py::Booking::test_cancel"
    ).json()
    assert detail["attachments"] == ["shot.png"]


def test_quarantine_endpoint_starts_empty_and_reflects_additions(client, engine, seeded):
    from testpulse_core import quarantine
    from testpulse_core.storage.db import session_scope

    empty = client.get("/api/suites/admin-e2e/quarantine").json()
    assert empty["entries"] == []
    assert empty["debt_count"] == 0

    with session_scope(engine) as session:
        quarantine.add(
            session,
            "admin-e2e",
            "tests/booking.py::Booking::test_reschedule",
            expires_after_days=14,
            reason="flaky on CI",
        )

    listed = client.get("/api/suites/admin-e2e/quarantine").json()
    assert len(listed["entries"]) == 1
    assert listed["entries"][0]["reason"] == "flaky on CI"
    assert listed["entries"][0]["is_expired"] is False


def test_quarantine_status_shows_up_on_the_test_itself(client, engine, seeded):
    from testpulse_core import quarantine
    from testpulse_core.storage.db import session_scope

    with session_scope(engine) as session:
        quarantine.add(
            session,
            "admin-e2e",
            "tests/booking.py::Booking::test_reschedule",
            expires_after_days=14,
        )

    items = client.get("/api/suites/admin-e2e/tests").json()["items"]
    by_name = {item["display_name"]: item for item in items}
    assert by_name["test_reschedule"]["is_quarantined"] is True
    assert by_name["test_create"]["is_quarantined"] is False


def test_pass_rate_is_null_not_zero_for_an_all_skipped_test(client, engine):
    from datetime import timedelta

    from testpulse_core.models import TestResult, TestRun, TestStatus
    from testpulse_core.storage.db import session_scope
    from testpulse_core.storage.repository import store_run

    from conftest import START

    for index in range(3):
        run = TestRun(
            suite_name="skips",
            started_at=START + timedelta(hours=index),
            finished_at=START + timedelta(hours=index, minutes=1),
            source_format="junit",
            commit_sha=f"sha{index}",
            results=[
                TestResult(
                    test_id="a.py::C::t",
                    display_name="t",
                    status=TestStatus.SKIPPED,
                    duration_ms=0,
                    raw_status="skipped",
                )
            ],
        )
        with session_scope(engine) as session:
            store_run(session, run)

    items = client.get("/api/suites/skips/tests").json()["items"]
    assert items[0]["pass_rate"] is None
    assert items[0]["scored_runs"] == 0


def test_failure_clusters_group_by_root_cause(client, seeded):
    clusters = client.get("/api/suites/admin-e2e/failures").json()
    assert clusters, "seeded data has failures with messages"
    assert clusters[0]["count"] >= 1
    assert clusters[0]["representative"] == "boom"
    assert clusters[0]["test_ids"]


def test_failure_clusters_for_an_unknown_suite_is_404(client, seeded):
    assert client.get("/api/suites/nope/failures").status_code == 404
