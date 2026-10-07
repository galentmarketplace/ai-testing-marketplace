"""The Performance playbook must test a build it deployed, and show what it measured.

Two defects this pins:

  * the playbook declared `mode:'perf'`, which resolves to tracks ['perf'] — the deploy
    agent is `tracks=("build","deploy")`, so it was never scheduled. The load test then
    fell back to `inputs.base_url`, and with a saved Configuration active that can be a
    THIRD-PARTY host: the SauceDemo Configuration would have pointed an open-model k6
    arrival-rate test at saucedemo.com. docs/DEMO.md meanwhile said to run it "with the
    deploy track enabled", a toggle that did not exist.
  * the run measured p95, p99, throughput, error rate and Core Web Vitals, and the Results
    screen showed one line of it: `· p95 2ms · err 0.0%` appended to a suite row.
"""
from pathlib import Path

import pytest

HTML = Path("web/static/index.html").read_text()
CARD = HTML.split("{id:'perf',", 1)[1].split("]},", 1)[0]
INTAKE = HTML.split("  perf:{fields:[", 1)[1].split("]},", 1)[0]


# ---------------------------------------------------------------- the playbook
def test_the_playbook_deploys_what_it_load_tests():
    assert "tracks:['perf','deploy']" in CARD


def test_the_deploy_choice_is_an_input():
    assert "FIELD('deploy'" in INTAKE


def test_the_base_url_field_says_when_it_applies():
    """Left ambiguous, a user points a load test at a host this run never deployed."""
    assert "only when deploy = no" in INTAKE


def test_the_card_describes_the_deploy_then_test_order():
    for claim in ("boot", "deployed", "deploy to no"):
        assert claim in CARD, f"the card never mentions {claim!r}"


def test_an_explicitly_selected_deploy_track_deploys():
    """Without this the playbook carried the track and still skipped the deployment."""
    from src.agents import deploy_agent
    st = {"run_config": {"tracks": ["perf", "deploy"]}, "story": {"inputs": {}}}
    assert deploy_agent._wanted(st, {}) is True


def test_an_explicit_no_still_wins_over_the_track():
    from src.agents import deploy_agent
    st = {"run_config": {"tracks": ["perf", "deploy"]}, "story": {"inputs": {}}}
    assert deploy_agent._wanted(st, {"deploy": "no"}) is False


def test_a_test_only_run_without_the_track_does_not_deploy():
    from src.agents import deploy_agent
    st = {"run_config": {"tracks": ["perf"]}, "story": {"inputs": {}}}
    assert deploy_agent._wanted(st, {}) is False


# ---------------------------------------------------------------- the report
def test_the_results_screen_has_a_performance_panel():
    assert 'id="perfReport"' in HTML and 'id="perfHead"' in HTML
    results = HTML.split("function renderResults()", 1)[1].split("\nfunction ", 1)[0]
    assert "renderPerformance();" in results


def test_the_panel_states_what_was_actually_tested():
    """A load test's first question is which host took the traffic."""
    body = HTML.split("function renderPerformance()", 1)[1].split("\nfunction ", 1)[0]
    assert "perf-target" in body
    assert "the build this run deployed" in body
    assert "did not build or boot it" in body, "a pre-existing target must be flagged as such"


def test_the_panel_reports_the_full_metric_set():
    body = HTML.split("function renderPerformance()", 1)[1].split("\nfunction ", 1)[0]
    for k in ("p95_ms", "p99_ms", "throughput_rps", "error_rate", "test_type"):
        assert k in body, f"{k} was measured and is not shown"


def test_the_thresholds_come_from_the_gate_not_a_duplicated_constant():
    """A threshold hard-coded in the UI drifts from the policy the gate applied."""
    body = HTML.split("function renderPerformance()", 1)[1].split("\nfunction ", 1)[0]
    assert "g.gate==='QG1'" in body
    assert "800" not in body, "the p95 threshold looks hard-coded in the view"


def test_latency_tiers_are_relative_to_the_policy():
    assert "function _perfTier(v, limit)" in HTML


def test_web_vitals_get_their_own_section():
    body = HTML.split("function renderPerformance()", 1)[1].split("\nfunction ", 1)[0]
    assert "Core Web Vitals" in body and "RUN.vitals" in body


def test_the_panel_inputs_are_accumulated():
    assert "if(u.perf_vitals) RUN.vitals=u.perf_vitals;" in HTML
    assert "if(u.deployment) RUN.deployment=u.deployment;" in HTML


# ---------------------------------------------------------------- the journey
def test_the_journey_has_a_deploy_stage():
    """The run booted the application and the timeline did not mention it."""
    assert "key:'deploy'" in HTML and "node:'deploy_app'" in HTML
    assert "deploy_app:'Deploy agent'" in HTML


def test_the_deploy_stage_names_the_url_it_booted():
    body = HTML.split("function renderJourney", 1)[1].split("\n// =====", 1)[0]
    assert "st.key==='deploy'" in body and "d.url" in body


def test_a_failed_deployment_is_reported_as_such():
    body = HTML.split("function renderJourney", 1)[1].split("\n// =====", 1)[0]
    assert "did not come up" in body


# ---------------------------------------------------------------- honesty details
def test_k6_checks_are_not_called_acceptance_cases():
    assert "'k6 checks':'cases'" in HTML


def test_a_gate_is_listed_on_every_flow_it_judges():
    """QG1 is drawn under the Playwright lane but also judges a perf-only run, so it was
    missing from 'Agents this flow will run' on exactly that flow."""
    body = HTML.split("function renderScopeSummary()", 1)[1].split("\nfunction ", 1)[0]
    assert "ch.tracks" in body and "active.has(t)" in body


def test_children_carry_their_own_tracks_in_the_manifest():
    from src.registry import build_manifest
    m = build_manifest()
    qg1 = next(ch for c in m["columns"] for ch in c["children"] if ch["id"] == "qg1")
    assert "perf" in qg1["tracks"] and "functional" in qg1["tracks"]


def test_the_runbook_no_longer_asks_for_a_deploy_toggle():
    demo = Path("docs/DEMO.md").read_text()
    assert "with the deploy track enabled" not in demo


if __name__ == "__main__":     # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))


def test_one_agent_is_listed_once_even_with_two_display_nodes():
    """execute_scripts is "Feature Runner" under Playwright and "Perf Runner (k6)" under
    Performance; listing both made one agent look like two."""
    html = Path("web/static/index.html").read_text()
    body = html.split("function renderScopeSummary()", 1)[1].split("\nfunction ", 1)[0]
    assert "c.agent||c.id||c.label" in body

    from src.registry import build_manifest
    kids = [ch for c in build_manifest()["columns"] for ch in c["children"]]
    assert all("agent" in ch for ch in kids), "children carry no backend agent id to dedupe on"


def test_the_decisive_inputs_are_not_hidden_in_the_optional_fold():
    """`deploy` decides WHICH HOST takes the load; it cannot sit behind a collapsed fold."""
    html = Path("web/static/index.html").read_text()
    intake = html.split("  perf:{fields:[", 1)[1].split("]},", 1)[0]
    for fid in ("deploy", "load", "perf_type"):
        line = next(ln for ln in intake.splitlines() if f"FIELD('{fid}'" in ln)
        assert line.rstrip().endswith("true),"), f"{fid} is not marked primary"
    body = html.split("function renderIntake()", 1)[1].split("\nfunction ", 1)[0]
    assert "f.required||f.primary" in body


def test_the_optional_fold_names_what_it_contains():
    html = Path("web/static/index.html").read_text()
    body = html.split("function renderIntake()", 1)[1].split("\nfunction ", 1)[0]
    assert "app URL, login, scope" not in body, "the fold advertised a fixed example list"
