"""Behaviour on input that is broken rather than merely empty.

A parser's error behaviour matters more than its happy path here. TestPulse
exists to tell teams which tests are failing, so a parser that quietly returns
fewer results than the file contained does not produce a smaller answer — it
produces a wrong one, and the missing tests are indistinguishable from tests
that were removed from the suite.
"""

from __future__ import annotations

import pytest

from conftest import FIXTURES
from testpulse_core.models import RunMetadata
from testpulse_core.parsers.allure import AllureParser
from testpulse_core.parsers.base import ParseError
from testpulse_core.parsers.junit import JUnitParser
from testpulse_core.parsers.playwright_json import PlaywrightJsonParser
from testpulse_core.parsers.pytest_json import PytestJsonParser

META = RunMetadata(suite_name="broken-suite")
MALFORMED = FIXTURES / "malformed"


def test_truncated_junit_is_rejected_rather_than_partially_read():
    # The most dangerous malformed case in this format. JUnit encodes "passed"
    # as the absence of a failure element, so a file cut off mid-document parses
    # into a shorter suite where every surviving test looks green. Salvaging it
    # would turn a broken upload into a clean run.
    with pytest.raises(ParseError, match="not well-formed XML"):
        JUnitParser().parse(MALFORMED / "truncated.xml", META)


def test_well_formed_xml_of_the_wrong_type_is_rejected():
    # A coverage report is valid XML. Without a root check it parses to zero
    # tests, which reads downstream as "the suite ran and had nothing in it".
    with pytest.raises(ParseError, match="expected <testsuites> or <testsuite>"):
        JUnitParser().parse(MALFORMED / "wrong-root.xml", META)


def test_testcase_without_a_name_is_rejected_with_its_suite_named():
    # Refusing is the easy part; saying where is what makes the error useful.
    with pytest.raises(ParseError, match="no name attribute") as excinfo:
        JUnitParser().parse(MALFORMED / "testcase-without-name.xml", META)
    assert "suite-a" in str(excinfo.value)


def test_directory_passed_where_a_file_is_expected(tmp_path):
    with pytest.raises(ParseError, match="not found"):
        JUnitParser().parse(tmp_path, META)


def test_allure_given_a_file_instead_of_a_directory():
    with pytest.raises(ParseError, match="is not a directory"):
        AllureParser().parse(MALFORMED / "truncated.xml", META)


def test_empty_allure_directory_is_an_error(tmp_path):
    with pytest.raises(ParseError, match=r"No \*-result\.json files"):
        AllureParser().parse(tmp_path, META)


def test_allure_directory_where_one_file_is_corrupt(tmp_path):
    # One good file and one bad one. The run is rejected outright: a partial
    # ingest here would silently drop whichever tests were in the bad file.
    source = next((FIXTURES / "allure" / "pytest-producer").glob("*.json"))
    (tmp_path / "a-result.json").write_text(source.read_text())
    (tmp_path / "b-result.json").write_text('{"name": "half writ')
    with pytest.raises(ParseError, match="not valid JSON"):
        AllureParser().parse(tmp_path, META)


def test_playwright_report_truncated_mid_json(tmp_path):
    partial = (FIXTURES / "playwright" / "playwright-report.json").read_text()[:400]
    target = tmp_path / "partial.json"
    target.write_text(partial)
    with pytest.raises(ParseError, match="not valid JSON"):
        PlaywrightJsonParser().parse(target, META)


def test_each_json_parser_rejects_the_other_format(tmp_path):
    # Both are valid JSON with a plausible shape, so only a structural check
    # separates them. Getting this wrong yields an empty run, not an error.
    playwright_report = FIXTURES / "playwright" / "playwright-report.json"
    pytest_report = FIXTURES / "pytest" / "pytest-report.json"

    with pytest.raises(ParseError, match="does not look like a pytest-json-report"):
        PytestJsonParser().parse(playwright_report, META)

    with pytest.raises(ParseError, match="does not look like a Playwright JSON report"):
        PlaywrightJsonParser().parse(pytest_report, META)


def test_empty_file_is_rejected_by_every_parser(tmp_path):
    empty = tmp_path / "empty"
    empty.write_text("")
    for parser in (JUnitParser(), PlaywrightJsonParser(), PytestJsonParser()):
        with pytest.raises(ParseError):
            parser.parse(empty, META)
