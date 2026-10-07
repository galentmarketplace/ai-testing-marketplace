"""The Results screen must actually show the coverage report.

Live defect: a coverage run finished green and the Results screen showed one journey row
with a sentence of gate text. The report, LCOV and Cobertura were written to disk and the
numbers were in the run state, but `accumulate()` never stored `coverage_report` and
`coverage_artifacts` was the one artifact list never pushed into `allArtifacts` — so there
was no coverage panel and no way to open the report from the UI at all.

These are source-level guards. The screen is vanilla JS with no test runner, and the three
facts below are exactly what was missing; each one silently empties the panel.
"""
from pathlib import Path

import pytest

HTML = Path("web/static/index.html").read_text()


def test_coverage_artifacts_are_pushed_into_the_artifact_list():
    """Every other *_artifacts key was pushed; this one was forgotten, so the report that
    the run had just written could not be opened from the screen."""
    assert "if(u.coverage_artifacts) _pushArts(u.coverage_artifacts);" in HTML


def test_every_artifact_list_the_agents_produce_reaches_the_ui():
    for key in ("code_artifacts", "test_artifacts", "security_artifacts", "functional_artifacts",
                "unit_artifacts", "a11y_artifacts", "contract_artifacts", "coverage_artifacts"):
        assert f"if(u.{key}) _pushArts(u.{key});" in HTML, f"{key} never reaches the Results screen"


def test_the_coverage_report_is_accumulated_into_run_state():
    assert "if(u.coverage_report) RUN.coverage=u.coverage_report;" in HTML


def test_the_renderer_is_called_when_results_are_drawn():
    results = HTML.split("function renderResults()", 1)[1].split("\nfunction ", 1)[0]
    assert "renderCoverage();" in results


def test_the_panel_has_a_container_to_render_into():
    assert 'id="covReport"' in HTML and 'id="covHead"' in HTML


def test_the_renderer_exists_and_handles_an_unmeasured_report():
    body = HTML.split("function renderCoverage()", 1)[1].split("\nfunction ", 1)[0]
    assert "if(!rep || !rep.ok)" in body, "a report that could not be measured must not throw"
    assert "rep.error" in body, "an unmeasured run should say why, not render nothing"


def test_the_panel_shows_before_and_after_not_only_the_final_number():
    body = HTML.split("function renderCoverage()", 1)[1].split("\nfunction ", 1)[0]
    assert "cov-move" in body and "delta" in body


def test_the_panel_reports_what_is_still_uncovered():
    body = HTML.split("function renderCoverage()", 1)[1].split("\nfunction ", 1)[0]
    assert "Still uncovered" in body
    assert "uncovered_funcs" in body


def test_a_failed_generation_is_shown_on_the_screen_too():
    body = HTML.split("function renderCoverage()", 1)[1].split("\nfunction ", 1)[0]
    assert "failed attempt, not a decision that the gap is acceptable" in body


def test_declined_functions_keep_their_reason_on_screen():
    body = HTML.split("function renderCoverage()", 1)[1].split("\nfunction ", 1)[0]
    assert "cov-skips" in body and "k.reason" in body


def test_the_coverage_tiers_match_the_standard_thresholds():
    """Green/amber/red must mean the same here as in every other coverage tool."""
    assert "function _covTier(pct){ return pct>=80?'good':(pct>=50?'part':'poor'); }" in HTML


def test_the_tier_classes_are_all_styled():
    for sel in (".cov-score.good", ".cov-score.part", ".cov-score.poor",
                ".covbar.good > span", ".covbar.part > span", ".covbar.poor > span"):
        assert sel in HTML, f"{sel} has no style, so that tier renders uncoloured"


def test_generated_tests_are_not_credited_to_the_measuring_agent():
    body = HTML.split("function renderJourney", 1)[1].split("\n// =====", 1)[0]
    assert "Go Test Generation agent" in body, \
        "the generated tests were listed as artifacts of the Go Coverage agent"


if __name__ == "__main__":     # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
