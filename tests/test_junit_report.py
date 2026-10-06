"""JUnit export: a green report must still say WHICH acceptance case passed.

Jenkins, GitHub and Azure all ingest JUnit, and that report is what a reviewer reads when
deciding whether a run proved anything. Two things make it lie:

  * synthesising "feature case #1" for passes, so a green run is untraceable to the
    acceptance case it supposedly verified
  * omitting skipped cases, so "never executed" is indistinguishable from "passed"
"""
import xml.etree.ElementTree as ET

import pytest

from src.integration import reports

CASES = [{"name": "FC-1 valid login", "status": "passed"},
         {"name": "FC-2 bad password", "status": "failed"},
         {"name": "FC-3 empty form", "status": "passed"},
         {"name": "FC-6 locked account", "status": "skipped"}]
RESULT = {"suite": "feature", "passed": 2, "failed": 1, "skipped": 1,
          "failures": [{"test": "FC-2 bad password", "error": "expected an error banner\nat line 3"}],
          "cases": CASES}


def _suite(xml):
    root = ET.fromstring(xml)
    return root.find("testsuite")


def test_real_case_titles_survive_into_the_report():
    ts = _suite(reports.junit_xml(RESULT))
    names = [tc.get("name") for tc in ts.iter("testcase")]
    assert names == ["FC-1 valid login", "FC-2 bad password", "FC-3 empty form",
                     "FC-6 locked account"]
    assert not any("case #" in n for n in names), "a passing case was synthesised"


def test_counts_include_skips_and_match_the_cases():
    ts = _suite(reports.junit_xml(RESULT))
    assert ts.get("tests") == "4"
    assert ts.get("failures") == "1"
    assert ts.get("skipped") == "1"


def test_a_skipped_case_is_marked_skipped_not_passed():
    ts = _suite(reports.junit_xml(RESULT))
    tc = next(t for t in ts.iter("testcase") if t.get("name") == "FC-6 locked account")
    assert tc.find("skipped") is not None
    assert tc.find("failure") is None


def test_a_failure_carries_its_real_error():
    ts = _suite(reports.junit_xml(RESULT))
    tc = next(t for t in ts.iter("testcase") if t.get("name") == "FC-2 bad password")
    f = tc.find("failure")
    assert f is not None and "expected an error banner" in f.get("message")


def test_passing_cases_carry_no_failure_element():
    ts = _suite(reports.junit_xml(RESULT))
    for name in ("FC-1 valid login", "FC-3 empty form"):
        tc = next(t for t in ts.iter("testcase") if t.get("name") == name)
        assert tc.find("failure") is None and tc.find("skipped") is None


def test_a_counts_only_runner_still_declares_its_skips():
    """Some runners report totals, not titles. 'Not run' must not read as 'passed'."""
    ts = _suite(reports.junit_xml({"suite": "unit", "passed": 2, "failed": 0,
                                   "skipped": 3, "failures": []}))
    assert ts.get("tests") == "5" and ts.get("skipped") == "3"
    assert sum(1 for t in ts.iter("testcase") if t.find("skipped") is not None) == 3


def test_the_report_is_wellformed_with_hostile_titles():
    """Generated titles contain quotes, angle brackets and ampersands."""
    xml = reports.junit_xml({
        "suite": "feature", "passed": 1, "failed": 1, "skipped": 0,
        "failures": [{"test": 'needs <b>"escaping" & care</b>', "error": "a < b & c > d"}],
        "cases": [{"name": "plain", "status": "passed"},
                  {"name": 'needs <b>"escaping" & care</b>', "status": "failed"}]})
    ts = _suite(xml)                                    # would raise on malformed XML
    assert any('"escaping"' in (t.get("name") or "") for t in ts.iter("testcase"))


def test_an_empty_result_is_still_valid_xml():
    ts = _suite(reports.junit_xml({"suite": "feature", "passed": 0, "failed": 0, "skipped": 0}))
    assert ts.get("tests") == "0"


if __name__ == "__main__":     # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))


# ---- XML-illegal characters: Playwright colours its failures with ANSI escapes ----
def test_ansi_escapes_do_not_break_the_report():
    """A raw 0x1b makes the file unparseable, so CI ingests nothing and a RED run
    shows up as 'no tests ran' — a false pass by omission."""
    ansi = "\x1b[2mexpect(\x1b[22m\x1b[31mlocator\x1b[39m).toBeVisible() failed"
    xml = reports.junit_xml({
        "suite": "feature", "passed": 0, "failed": 1, "skipped": 0,
        "failures": [{"test": "FC-1 login", "error": ansi}],
        "cases": [{"name": "FC-1 login", "status": "failed"}]})
    ts = _suite(xml)                                  # raises if malformed
    msg = next(t for t in ts.iter("testcase")).find("failure").get("message")
    assert "\x1b" not in msg and "expect(" in msg


def test_control_characters_are_stripped_from_titles():
    xml = reports.junit_xml({
        "suite": "feature", "passed": 1, "failed": 0, "skipped": 0,
        "cases": [{"name": "case with \x00 and \x07 in it", "status": "passed"}]})
    ts = _suite(xml)
    name = next(t for t in ts.iter("testcase")).get("name")
    assert "\x00" not in name and "\x07" not in name


def test_the_counts_only_path_is_also_sanitised():
    xml = reports.junit_xml({
        "suite": "feature", "passed": 0, "failed": 1, "skipped": 0,
        "failures": [{"test": "t\x1b[31m", "error": "boom\x1b[0m"}]})
    _suite(xml)                                       # raises if malformed
