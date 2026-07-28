"""Failure clustering tests.

The claim being tested is precision: things that group are genuinely the same
problem. The last section tests the known false-merge case too, because a
documented limitation that has no test is just a comment.
"""

from __future__ import annotations

from testpulse_core.clustering import cluster_failures, normalise


def test_timeouts_differing_only_in_duration_group_together():
    a = "TimeoutError: locator.waitFor: Timeout 5000ms exceeded."
    b = "TimeoutError: locator.waitFor: Timeout 30000ms exceeded."
    assert normalise(a) == normalise(b)


def test_messages_differing_by_a_uuid_group_together():
    a = "Booking 3f2504e0-4f89-11d3-9a0c-0305e82c3301 not found"
    b = "Booking 7c9e6679-7425-40de-944b-e07fc1f90ae7 not found"
    assert normalise(a) == normalise(b)


def test_messages_differing_by_a_source_location_group_together():
    a = "AssertionError\n  at tests/booking.spec.ts:65:7"
    b = "AssertionError\n  at tests/booking.spec.ts:118:3"
    assert normalise(a) == normalise(b)


def test_messages_differing_by_a_quoted_value_group_together():
    a = "Expected element 'submit-button' to be visible"
    b = "Expected element 'cancel-button' to be visible"
    assert normalise(a) == normalise(b)


def test_messages_differing_by_a_timestamp_group_together():
    a = "Slot unavailable at 2026-07-01T10:00:00Z"
    b = "Slot unavailable at 2026-11-14T22:31:05Z"
    assert normalise(a) == normalise(b)


def test_genuinely_different_failures_stay_apart():
    a = "TimeoutError: locator.waitFor: Timeout 5000ms exceeded."
    b = "AssertionError: expected status 200 but got 500"
    assert normalise(a) != normalise(b)


def test_a_uuid_is_not_shredded_by_the_hex_rule():
    # Ordering bug bait: the generic hex rule would turn a UUID into a string of
    # <hex> fragments, and two UUIDs with different dash placement would then
    # normalise differently.
    assert normalise("id 3f2504e0-4f89-11d3-9a0c-0305e82c3301 missing") == "id <uuid> missing"


def test_durations_normalise_consistently_regardless_of_spacing():
    assert normalise("took 5000ms") == normalise("took 5000 ms")


def test_clusters_are_ordered_largest_first():
    failures = [
        ("t1", "Timeout 100ms exceeded"),
        ("t2", "Timeout 200ms exceeded"),
        ("t3", "Timeout 300ms exceeded"),
        ("t4", "Connection refused"),
    ]
    clusters = cluster_failures(failures)
    assert clusters[0].count == 3
    assert clusters[1].count == 1


def test_a_cluster_reports_which_tests_produced_it():
    # A cluster spanning many tests points at shared infrastructure rather than
    # at any one test.
    failures = [
        ("suite/a.py::C::t1", "Timeout 100ms exceeded"),
        ("suite/b.py::C::t2", "Timeout 900ms exceeded"),
    ]
    cluster = cluster_failures(failures)[0]
    assert cluster.test_ids == ["suite/a.py::C::t1", "suite/b.py::C::t2"]


def test_the_representative_is_a_real_unedited_message():
    # The template alone is not debuggable. Somebody needs to see an actual
    # failure, not a version with the interesting parts replaced.
    failures = [("t1", "Timeout 5000ms exceeded"), ("t2", "Timeout 5000ms exceeded")]
    cluster = cluster_failures(failures)[0]
    assert cluster.representative == "Timeout 5000ms exceeded"
    assert "<duration>" in cluster.template
    assert "<duration>" not in cluster.representative


def test_the_representative_is_the_most_common_variant():
    failures = [
        ("t1", "Timeout 5000ms exceeded"),
        ("t2", "Timeout 5000ms exceeded"),
        ("t3", "Timeout 99ms exceeded"),
    ]
    assert cluster_failures(failures)[0].representative == "Timeout 5000ms exceeded"


def test_empty_messages_do_not_form_a_cluster():
    # A failure with no message is a reporting gap, not a root cause. Letting it
    # cluster would put it at the top and push real problems down.
    failures = [("t1", ""), ("t2", "   "), ("t3", "Connection refused")]
    clusters = cluster_failures(failures)
    assert len(clusters) == 1
    assert clusters[0].representative == "Connection refused"


def test_limit_caps_the_number_of_clusters():
    failures = [(f"t{i}", f"Distinct failure kind {chr(65 + i)}") for i in range(10)]
    assert len(cluster_failures(failures, limit=3)) == 3


def test_the_documented_false_merge_really_happens():
    # Stated as a known limitation, so it gets a test. Two different assertions
    # that differ only in a number collapse into one cluster. Usually right,
    # occasionally not, and worth knowing before trusting a count.
    a = "AssertionError: expected 3 items"
    b = "AssertionError: expected 7 items"
    assert normalise(a) == normalise(b)
    assert len(cluster_failures([("t1", a), ("t2", b)])) == 1


def test_real_playwright_messages_from_the_fixtures_cluster_sensibly():
    # Taken verbatim from the orangehrm Allure results.
    failures = [
        (
            "recruitment.spec.ts::R::Delete a vacancy",
            "TimeoutError: locator.waitFor: Timeout 5000ms exceeded.\n"
            "Call log:\n  - waiting for locator('.oxd-select-option').nth(1) to be visible\n",
        ),
        (
            "recruitment.spec.ts::R::Edit a vacancy",
            "TimeoutError: locator.waitFor: Timeout 5000ms exceeded.\n"
            "Call log:\n  - waiting for locator('.oxd-select-option').nth(2) to be visible\n",
        ),
        ("recruitment.spec.ts::R::Add a vacancy", "Test timeout of 30000ms exceeded."),
    ]
    clusters = cluster_failures(failures)
    # The two locator timeouts are one problem; the whole-test timeout is another.
    assert len(clusters) == 2
    assert clusters[0].count == 2
    assert len(clusters[0].test_ids) == 2
