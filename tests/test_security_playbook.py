"""The security scan must report what ran, what it found, and what could NOT run.

Three defects this pins, all found by running the Security Audit playbook and reading the
screen it produced:

  * `run_security_scan` appended a run result with `passed = files_scanned`, so 163 scanned
    files became "163 passed tests" and the Results screen announced "100% tests passed" on
    a run that had just found 67 high-severity issues. Same family as `passed or 1`.
  * the Results screen showed `high / medium / low` counts only, so a CRITICAL finding — the
    one severity that stops a release — appeared nowhere, neither in the summary card nor in
    the journey line.
  * the report lived inside the collapsed "Raw details" fold, and never said which of the six
    disciplines had actually run. A scanner that is absent must read as NOT RUN, never as a
    discipline that found nothing.
"""
from pathlib import Path

import pytest

HTML = Path("web/static/index.html").read_text()
CARD = HTML.split("{id:'security',", 1)[1].split("]},", 1)[0]
PANEL = HTML.split("function renderSecurityReport()", 1)[1].split("\nfunction ", 1)[0]


# ------------------------------------------------- a scan is not a test suite
def test_the_scan_does_not_manufacture_a_pass_count():
    src = Path("src/agents/security_agent.py").read_text()
    code = [ln for ln in src.splitlines() if not ln.strip().startswith("#")]
    assert not any('"passed": report["files_scanned"]' in ln for ln in code), \
        "files scanned is being reported as tests passed"


def test_the_scan_emits_no_run_result_at_all():
    """It reports through security_report and the SEC gate, not through a suite row."""
    from src.agents import security_agent
    src = __import__("inspect").getsource(security_agent.run_security_scan)
    code = [ln for ln in src.splitlines() if not ln.strip().startswith("#")]
    assert not any("run_results" in ln for ln in code)


def test_a_security_run_shows_findings_where_the_pass_rate_tile_was():
    assert "k:'Findings'" in HTML


# ------------------------------------------------- severity must be complete
def test_every_severity_including_critical_is_rendered():
    assert "const _SEVS = ['critical','high','medium','low','info'];" in HTML
    assert "_SEVS.map" in PANEL, "the panel does not iterate the full severity set"


def test_the_journey_line_no_longer_drops_critical():
    body = HTML.split("function renderJourney", 1)[1].split("\n// =====", 1)[0]
    sec = body.split("st.key==='sec'", 1)[1][:600]
    assert "_SEVS" in sec
    assert "s.high} high / ${s.medium} med" not in sec


def test_the_severity_chips_are_all_styled():
    for sev in ("critical", "high", "medium", "low", "info"):
        assert f".sev-chip.{sev}" in HTML, f"{sev} renders uncoloured"


def test_critical_or_high_is_never_coloured_as_good():
    assert "function _secTier(s)" in HTML
    body = HTML.split("function _secTier(s)", 1)[1].split("\n", 4)[0:4]
    assert "critical" in "".join(body) and "high" in "".join(body)


# ------------------------------------------------- the point of the panel
def test_the_panel_is_first_class_not_inside_the_raw_details_fold():
    assert 'id="secReport"' in HTML and 'id="secHead"' in HTML
    head, fold = HTML.split('<details class="details-fold">', 1)
    assert 'id="secReport"' in head, "the security panel is buried in the collapsed fold"
    results = HTML.split("function renderResults()", 1)[1].split("\nfunction ", 1)[0]
    assert "renderSecurityReport();" in results


def test_every_methodology_is_listed_whether_it_ran_or_not():
    assert "NOT RUN" in PANEL
    assert "m.ran" in PANEL and "r.methodologies" in PANEL


def test_a_discipline_that_could_not_run_explains_why_and_how_to_enable_it():
    assert "m.note" in PANEL and "m.install" in PANEL


def test_missing_disciplines_are_not_presented_as_clean():
    assert "their findings are unknown, not zero" in PANEL


def test_a_scan_that_failed_outright_is_not_shown_as_a_clean_result():
    assert "Nothing was verified — this is not a clean result" in PANEL


def test_findings_carry_the_standards_a_reviewer_triages_by():
    for k in ("f.cwe", "f.owasp", "f.cvss"):
        assert k in PANEL, f"{k} is collected by the scanner and not shown"


def test_findings_are_ordered_worst_first():
    assert "order(a.severity)-order(b.severity)" in PANEL


def test_the_exports_are_reachable():
    assert "RUN.securityArts" in PANEL and "viewArtifact" in PANEL


# ------------------------------------------------- the playbook screen
def test_the_card_names_the_six_disciplines():
    for d in ("static analysis", "dependencies", "secrets", "IaC", "container", "bill of materials"):
        assert d.lower() in CARD.lower(), f"the card never mentions {d}"


def test_the_card_states_the_graceful_degrade_promise():
    assert "NOT RUN" in CARD and "never as clean" in CARD


def test_the_card_chips_list_the_disciplines_and_the_export():
    for chip in ("SAST", "SCA / CVE", "Secrets", "IaC", "Container", "SBOM", "SARIF"):
        assert f"'{chip}'" in CARD


def test_the_scan_scope_is_shown_not_folded():
    intake = HTML.split("  security:{fields:[", 1)[1].split("]},", 1)[0]
    assert intake.rstrip().rstrip(")").endswith("true"), \
        "the scan scope is not marked primary, so it sits in the collapsed fold"


if __name__ == "__main__":     # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
