"""The Insights agent: a model writes ABOUT figures it did not produce.

Julius AI's pattern — "data analysis without the formulas" — applied to this platform's own
run history. The split is the whole point and each half is pinned here:

  * `analytics.summarize` computes every figure. No model touches arithmetic.
  * the model writes prose, and `unverified_figures` checks it. A number in the write-up that
    is not in the computed data is a fabrication, which is a false pass in a nicer typeface.
  * simulated runs are excluded outright, and runs whose provenance is unknown are reported
    as unknown rather than counted as measurements.
"""
from pathlib import Path

import pytest

from src.integration import analytics

HTML = Path("web/static/index.html").read_text()


def _run(num, status="done", mock=None, **extra):
    return {"num": num, "id": f"r{num}", "flow": extra.pop("flow", "Go Code Coverage"),
            "tracks": ["coverage"], "status": status, "created_at": 100.0,
            "updated_at": 100.0 + extra.pop("dur", 20.0), "mock": mock,
            "inputs": {"repo": extra.pop("repo", "org/go-orders-service")},
            "result": {"llm_usage": {"cost_usd": extra.pop("cost", 0.04)}}, **extra}


def _ev(update):
    return [{"type": "node", "payload": {"node": "x", "update": update}}]


# ------------------------------------------------------------------ folding
def test_a_run_folds_to_the_facts_worth_trending():
    r = analytics.fold_run(_run(5), _ev({
        "coverage_report": {"ok": True, "total_pct": 28.6, "total_pct_after": 96.4,
                            "after": {"total_pct": 96.4}, "min_pct": 80},
        "gate_decisions": [{"gate": "COVERAGE", "verdict": "fail"},
                           {"gate": "COVERAGE", "verdict": "pass"}]}))
    assert r["coverage"] == {"before": 28.6, "after": 96.4, "min_pct": 80}
    assert r["gates"] == {"COVERAGE": "pass"}          # the FINAL verdict, not the first
    assert r["gate_attempts"] == {"COVERAGE": 2}
    assert r["self_corrections"] == 1
    assert r["duration_s"] == 20.0 and r["cost_usd"] == 0.04


def test_provenance_comes_from_the_run_row_and_distinguishes_unknown():
    assert analytics.fold_run(_run(1, mock=1), [])["mock"] is True
    assert analytics.fold_run(_run(2, mock=0), [])["mock"] is False
    assert analytics.fold_run(_run(3, mock=None), [])["mock"] is None


# ------------------------------------------------------------------ simulated data
def test_simulated_runs_are_excluded_from_every_figure():
    """The mock runner emits p95 640ms and 74.2% coverage. Averaged in with measurements
    they are indistinguishable from them."""
    real = [analytics.fold_run(_run(n, mock=0), _ev({"run_results": [
        {"suite": "feature", "passed": 1, "perf": {"p95_ms": 2.0}}]})) for n in (1, 2)]
    fake = [analytics.fold_run(_run(n, mock=1), _ev({"run_results": [
        {"suite": "feature", "passed": 1, "perf": {"p95_ms": 640.0}}]})) for n in (3, 4)]
    f = analytics.summarize(real + fake)
    assert f["runs_analysed"] == 2 and f["mock_runs_excluded"] == 2
    assert f["performance"]["worst_p95_ms"] == 2.0, "a simulated latency reached the trend"


def test_a_history_of_nothing_but_simulations_reports_that():
    f = analytics.summarize([analytics.fold_run(_run(n, mock=1), []) for n in (1, 2)])
    assert f["ok"] is False and "mock" in f["error"]


def test_runs_predating_provenance_are_counted_as_unknown_not_real():
    f = analytics.summarize([analytics.fold_run(_run(n, mock=None), []) for n in (1, 2, 3)])
    assert f["runs_analysed"] == 3 and f["provenance_unknown"] == 3
    assert f["mock_runs_excluded"] == 0


# ------------------------------------------------------------------ the figures
def test_completion_and_block_rates_are_computed_per_flow():
    rs = [analytics.fold_run(_run(1, mock=0, flow="A"), []),
          analytics.fold_run(_run(2, "blocked", mock=0, flow="A"), []),
          analytics.fold_run(_run(3, mock=0, flow="B"), [])]
    f = analytics.summarize(rs)
    assert f["completion_rate_pct"] == 66.7
    a = next(x for x in f["flows"] if x["flow"] == "A")
    assert a["runs"] == 2 and a["completed"] == 1 and a["blocked"] == 1
    assert a["completion_rate_pct"] == 50.0


def test_the_gate_table_shows_which_gate_actually_fails():
    rs = [analytics.fold_run(_run(1, mock=0), _ev({"gate_decisions": [
              {"gate": "UNIT", "verdict": "fail"}]})),
          analytics.fold_run(_run(2, mock=0), _ev({"gate_decisions": [
              {"gate": "UNIT", "verdict": "pass"}, {"gate": "SEC", "verdict": "pass"}]}))]
    g = {x["gate"]: x for x in analytics.summarize(rs)["gates"]}
    assert g["UNIT"]["evaluated"] == 2 and g["UNIT"]["failed"] == 1
    assert g["UNIT"]["fail_rate_pct"] == 50.0 and g["SEC"]["failed"] == 0


def test_coverage_is_trended_per_repository():
    rs = [analytics.fold_run(_run(n, mock=0), _ev({"coverage_report": {
        "ok": True, "total_pct": 28.6, "total_pct_after": 96.4}})) for n in (1, 2)]
    c = analytics.summarize(rs)["coverage"][0]
    assert c["repo"] == "go-orders-service"
    assert c["baseline_pct"] == 28.6 and c["latest_pct"] == 96.4 and c["raised"] == 2


def test_an_empty_history_is_an_honest_failure_not_a_zero():
    f = analytics.summarize([])
    assert f["ok"] is False and "no runs" in f["error"]


# ------------------------------------------------------------------ the verifier
FACTS = {"completion_rate_pct": 79.0, "total_cost_usd": 9.06, "runs_analysed": 100,
         "coverage": [{"baseline_pct": 28.6, "latest_pct": 96.4,
                       "series": [{"run": 86}, {"run": 89}, {"run": 91}]}]}


def test_an_invented_figure_is_caught():
    assert analytics.unverified_figures("Completion sits at 42.7%.", FACTS) == ["42.7"]


def test_figures_that_were_computed_pass():
    assert analytics.unverified_figures(
        "79.0% of 100 runs completed, costing 9.06 in total; coverage moved 28.6 to 96.4.",
        FACTS) == []


def test_a_hyphenated_range_is_not_read_as_a_negative_number():
    """Live: the model wrote "runs 86-89, 91-98" and the checker reported -89 and -98 as
    invented. A false alarm teaches people to ignore the real ones."""
    assert analytics.unverified_figures("coverage rose in runs 86-89 and 91", FACTS) == []


def test_a_hyphen_inside_a_word_is_not_a_minus_sign():
    """Live: "a sub-80% completion rate" was reported as the invented figure -80."""
    assert analytics.unverified_figures("a sub-100 run sample", FACTS) == []


def test_a_genuine_negative_figure_is_still_checked():
    assert analytics.unverified_figures("The delta was -3.5 points.", FACTS) == ["-3.5"]


def test_a_rounded_restatement_of_a_real_figure_is_accepted():
    assert analytics.unverified_figures("about 79% of runs completed", FACTS) == []


def test_prose_with_no_numbers_is_clean():
    assert analytics.unverified_figures("Most runs complete; the slow ones deploy first.", FACTS) == []


# ------------------------------------------------------------------ the agent + gate
def test_the_figures_are_computed_without_the_model(monkeypatch):
    """If the model is ever asked to produce a number, this agent's premise is gone."""
    import inspect

    from src.agents import insights_agent
    src = inspect.getsource(analytics)
    assert "call_llm" not in src, "the computation module reaches for a model"
    assert "call_llm_json" in inspect.getsource(insights_agent), "the narrative is not model-written"


def test_the_gate_reports_unverified_figures_as_a_failed_check():
    from src.agents import insights_agent
    out = insights_agent.insights_gate({"gate_decisions": [], "insights_report": {
        "ok": True, "runs_analysed": 10, "unverified_figures": ["42.7"],
        "flows": [], "gates": [], "provenance_unknown": 0}})
    d = out["gate_decisions"][-1]
    chk = next(c for c in d["checks"] if "unverified" in c["label"])
    assert chk["ok"] is False and "42.7" in chk["label"]


def test_the_gate_passes_cleanly_when_nothing_was_invented():
    from src.agents import insights_agent
    out = insights_agent.insights_gate({"gate_decisions": [], "insights_report": {
        "ok": True, "runs_analysed": 10, "unverified_figures": [],
        "flows": [], "gates": [], "provenance_unknown": 0}})
    d = out["gate_decisions"][-1]
    assert d["verdict"] == "pass"
    assert any("no invented figures" in c["label"] for c in d["checks"])


def test_unknown_provenance_is_surfaced_by_the_gate():
    from src.agents import insights_agent
    out = insights_agent.insights_gate({"gate_decisions": [], "insights_report": {
        "ok": True, "runs_analysed": 10, "unverified_figures": [],
        "flows": [], "gates": [], "provenance_unknown": 9}})
    chk = next(c for c in out["gate_decisions"][-1]["checks"] if "provenance" in c["label"])
    assert chk["ok"] is False


def test_the_agent_is_registered_with_its_own_track_and_gate():
    from src.registry import build_manifest
    m = build_manifest()
    col = next(c for c in m["columns"] if c["id"] == "insights")
    assert col["tracks"] == ["insights"]
    assert any(ch["id"] == "insights_gate" for ch in col["children"])


# ------------------------------------------------------------------ the screen
def test_the_results_screen_has_an_insights_panel():
    assert 'id="insReport"' in HTML and 'id="insHead"' in HTML
    results = HTML.split("function renderResults()", 1)[1].split("\nfunction ", 1)[0]
    assert "renderInsights();" in results


def test_the_panel_separates_computed_figures_from_model_prose():
    body = HTML.split("function renderInsights()", 1)[1].split("\nfunction ", 1)[0]
    assert "unverified_figures" in body
    assert "model's wording, not measurements" in body
    assert "traceable to the computed data" in body


def test_the_panel_shows_what_was_excluded_next_to_the_number():
    body = HTML.split("function renderInsights()", 1)[1].split("\nfunction ", 1)[0]
    assert "mock_runs_excluded" in body and "provenance_unknown" in body
    assert "not assumed real" in body


def test_a_trend_needs_enough_points_to_be_a_trend():
    """The design guidance is explicit: fewer than four points is a stat card, not a line."""
    body = HTML.split("function renderInsights()", 1)[1].split("\nfunction ", 1)[0]
    assert "pts.length>=4" in body and "too few to trend" in body


def test_the_insights_artifacts_reach_the_screen():
    assert "if(u.insights_artifacts) _pushArts(u.insights_artifacts);" in HTML
    assert "if(u.insights_report) RUN.insights=u.insights_report;" in HTML


def test_the_playbook_promises_only_what_the_agent_does():
    card = HTML.split("{id:'insights',", 1)[1].split("]},", 1)[0]
    assert "tracks:['insights']" in card
    assert "computed from the run history" in card
    assert "unverified" in card


def test_the_question_and_depth_are_shown_not_folded():
    intake = HTML.split("  insights:{fields:[", 1)[1].split("]},", 1)[0]
    for fid in ("question", "history_limit"):
        line = next(ln for ln in intake.splitlines() if f"FIELD('{fid}'" in ln)
        assert line.rstrip().rstrip("),]}").endswith("true"), f"{fid} sits in the collapsed fold"


def test_the_journey_has_an_insights_stage():
    assert "key:'insights'" in HTML and "node:'analyze_runs'" in HTML
    assert "analyze_runs:'Insights agent'" in HTML


# ------------------------------------------------------------------ provenance plumbing
def test_the_store_records_whether_a_run_was_simulated():
    src = Path("web/store.py").read_text()
    assert "def _migrate_runs(c)" in src
    assert "ALTER TABLE runs ADD COLUMN mock INTEGER" in src
    assert "mock: bool | None = None" in src


def test_both_run_entry_points_record_provenance():
    src = Path("web/server.py").read_text()
    assert src.count("mock=bool(req.mock)") == 2, "a run can still be created without provenance"


if __name__ == "__main__":     # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))


def test_an_insights_run_counts_runs_where_the_pass_rate_tile_was():
    assert "k:'Runs analysed'" in HTML


def test_the_trends_carry_the_provenance_caveat():
    """A trend is only as trustworthy as the provenance of the runs feeding it."""
    body = HTML.split("function renderInsights()", 1)[1].split("\nfunction ", 1)[0]
    trends = body.split("Trends <span", 1)[1]
    assert "predate provenance tracking" in trends
    assert "640ms" in trends, "the caveat does not say what a simulated figure looks like"
