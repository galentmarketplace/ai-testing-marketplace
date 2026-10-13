"""The Data Analyst agent: the model writes the code, the CODE produces the answer.

This is the Julius-AI mechanism, and it is the mirror image of the Insights agent. There the
figures are computed by our code and the model only writes prose; here the model writes the
arithmetic — so the arithmetic must be EXECUTED, and nothing the model says about the data
may reach the report unexecuted.

What is pinned:
  * the answer is the script's stdout and the files it wrote, never a model summary;
  * a script that raises is reported as a failure with its traceback, after a bounded number
    of repair attempts — never as an answer;
  * the executed source ships, so every figure is traceable to the code that produced it;
  * the execution posture: isolated interpreter, fresh directory, the data copied in.
"""
import inspect
from pathlib import Path

import pytest

from src.agents import data_analyst_agent as da

HTML = Path("web/static/index.html").read_text()
SRC = Path("src/agents/data_analyst_agent.py").read_text()


# ------------------------------------------------- the answer must be executed
def test_the_report_carries_the_scripts_own_output():
    r = da._report_markdown({
        "ok": True, "question": "q", "source": "s", "data_file": "d.csv", "approach": "a",
        "code": "print(1)", "stdout": "rows: 42", "error": "", "charts": [], "attempts": 1})
    assert "rows: 42" in r
    assert "the script's own output" in r


def test_a_failed_script_reports_the_traceback_instead_of_an_answer():
    r = da._report_markdown({
        "ok": False, "question": "q", "source": "s", "data_file": "d.csv", "approach": "",
        "code": "boom", "stdout": "", "error": "ZeroDivisionError: division by zero",
        "charts": [], "attempts": 3})
    assert "Not answered" in r and "ZeroDivisionError" in r
    assert "no result" in r


def test_the_model_is_never_asked_to_interpret_the_results():
    """Any second LLM call after execution would reintroduce model-written numbers."""
    body = inspect.getsource(da.analyze_data)
    after = body.split("result = _execute(", 1)[1]
    assert "call_llm" not in after.split("break", 1)[1], \
        "the model is consulted after a successful run — its words would become the answer"


def test_the_executed_source_is_always_attached():
    assert '"type": "analysis-code"' in SRC
    r = da._report_markdown({"ok": True, "question": "q", "source": "s", "data_file": "d",
                             "approach": "", "code": "import pandas", "stdout": "x",
                             "error": "", "charts": [], "attempts": 1})
    assert "The code that produced this" in r and "import pandas" in r


# ------------------------------------------------- execution posture
def test_the_interpreter_runs_in_isolated_mode():
    """-I keeps the data directory and user site-packages off sys.path, so a file named
    json.py sitting beside the data cannot hijack an import."""
    assert '"-I"' in SRC


def test_the_script_runs_in_the_run_workspace_not_the_data_directory():
    assert 'cwd=str(work)' in SRC


def test_the_data_file_is_copied_so_the_original_cannot_be_modified():
    assert "shutil.copy2(src, out)" in SRC
    assert "COPIED" in SRC


def test_the_child_gets_no_secrets_and_a_headless_backend():
    assert "sandbox.child_env(" in SRC and '"MPLBACKEND": "Agg"' in SRC


def test_execution_is_bounded_by_a_timeout():
    assert "timeout=TIMEOUT_S" in SRC
    assert "subprocess.TimeoutExpired" in SRC


def test_the_repair_loop_is_bounded():
    assert "MAX_ATTEMPTS" in SRC
    assert "range(1, MAX_ATTEMPTS + 1)" in SRC


def test_the_repair_prompt_forbids_hiding_the_failure():
    assert "do not wrap the failure in try/except" in da.REPAIR
    assert "do not print a result the code did not compute" in da.REPAIR.replace("\n", " ")


def test_the_prompt_forbids_network_and_stray_writes():
    assert "must not access the network" in da.SYSTEM
    assert "stdout IS the answer" in da.SYSTEM


# ------------------------------------------------- real execution
def test_a_working_script_runs_and_its_output_is_captured(tmp_path):
    (tmp_path / "d.csv").write_text("a,b\n1,2\n3,4\n")
    out = da._execute("import sys, csv\n"
                      "rows=list(csv.DictReader(open(sys.argv[1])))\n"
                      "print('rows:', len(rows))\n", tmp_path, tmp_path / "d.csv")
    assert out["ok"] is True and "rows: 2" in out["stdout"]


def test_a_broken_script_is_a_failure_with_its_traceback(tmp_path):
    (tmp_path / "d.csv").write_text("a\n1\n")
    out = da._execute("raise ValueError('nope')", tmp_path, tmp_path / "d.csv")
    assert out["ok"] is False and "ValueError" in out["error"]
    assert out["stdout"] == ""


def test_charts_the_script_writes_are_collected(tmp_path):
    (tmp_path / "d.csv").write_text("a\n1\n")
    out = da._execute("open('chart.png','wb').write(b'\\x89PNG\\r\\n')\nprint('done')",
                      tmp_path, tmp_path / "d.csv")
    assert out["ok"] is True and out["charts"] == ["chart.png"]


def test_a_preexisting_png_is_not_claimed_as_a_new_chart(tmp_path):
    (tmp_path / "d.csv").write_text("a\n1\n")
    (tmp_path / "old.png").write_bytes(b"\x89PNG")
    out = da._execute("print('no charts here')", tmp_path, tmp_path / "d.csv")
    assert out["charts"] == [], "a chart from an earlier attempt was credited to this one"


def test_the_isolated_interpreter_ignores_a_planted_module(tmp_path):
    """A hostile file beside the data must not be importable as a stdlib name."""
    (tmp_path / "d.csv").write_text("a\n1\n")
    (tmp_path / "json.py").write_text("raise RuntimeError('hijacked')")
    out = da._execute("import json\nprint('safe:', json.dumps({'a': 1}))",
                      tmp_path, tmp_path / "d.csv")
    assert out["ok"] is True and "safe:" in out["stdout"]


# ------------------------------------------------- data sources
def test_a_local_file_is_copied_into_the_workspace(tmp_path):
    src = tmp_path / "orig.csv"
    src.write_text("a\n1\n")
    work = tmp_path / "w"
    work.mkdir()
    out, origin = da._fetch(str(src), work)
    assert out is not None and out.parent == work and out.read_text() == "a\n1\n"
    assert src.exists(), "the original was moved rather than copied"


def test_a_missing_file_is_reported_not_guessed(tmp_path):
    out, why = da._fetch(str(tmp_path / "nope.csv"), tmp_path)
    assert out is None and "no such file" in why


def test_a_hostile_filename_cannot_escape_the_workspace(tmp_path):
    bad = tmp_path / "weird name;rm -rf.csv"
    bad.write_text("a\n1\n")
    work = tmp_path / "w"
    work.mkdir()
    out, _ = da._fetch(str(bad), work)
    assert out is not None and out.parent == work
    assert ";" not in out.name and " " not in out.name


def test_the_profile_describes_the_real_columns(tmp_path):
    f = tmp_path / "d.csv"
    f.write_text("flow,duration_s\nalpha,1.5\nbeta,2.5\n")
    prof = da._profile(f)
    assert "flow" in prof and "duration_s" in prof and "2 rows" in prof


def test_an_unparseable_file_says_so_rather_than_inventing_columns(tmp_path):
    f = tmp_path / "d.csv"
    f.write_text("\x00\x01 not a table")
    assert "could not parse" in da._profile(f) or "First bytes" in da._profile(f)


# ------------------------------------------------- the gate
def test_the_gate_fails_when_no_script_ever_ran():
    d = da.analysis_gate({"gate_decisions": [], "analysis_report": {
        "ok": False, "attempts": 3, "error": "SyntaxError"}})["gate_decisions"][-1]
    assert d["verdict"] == "fail"
    assert any(not c["ok"] for c in d["checks"])


def test_the_gate_states_that_the_output_was_computed():
    d = da.analysis_gate({"gate_decisions": [], "analysis_report": {
        "ok": True, "attempts": 1, "stdout": "a\nb\nc", "charts": ["c.png"],
        "code": "print(1)"}})["gate_decisions"][-1]
    assert d["verdict"] == "pass"
    assert any("not the model" in c["label"] for c in d["checks"])
    assert any("auditable" in c["threshold"] for c in d["checks"])


def test_the_agent_is_registered_with_its_own_track_and_gate():
    from src.registry import build_manifest
    col = next(c for c in build_manifest()["columns"] if c["id"] == "analysis")
    assert col["tracks"] == ["analysis"]
    assert any(ch["id"] == "analysis_gate" for ch in col["children"])


def test_it_degrades_honestly_without_pandas():
    assert "pandas is not installed" in SRC


# ------------------------------------------------- the screen
def test_the_results_screen_has_an_analysis_panel():
    assert 'id="anaReport"' in HTML and 'id="anaHead"' in HTML
    results = HTML.split("function renderResults()", 1)[1].split("\nfunction ", 1)[0]
    assert "renderAnalysis();" in results


def test_the_panel_attributes_the_numbers_to_the_script():
    body = HTML.split("function renderAnalysis()", 1)[1].split("\nfunction ", 1)[0]
    assert "did not write the numbers" in body
    assert "printed by the script" in body


def test_the_panel_shows_a_failure_as_a_failure():
    body = HTML.split("function renderAnalysis()", 1)[1].split("\nfunction ", 1)[0]
    assert "There is no" in body and "would be a fabrication" in body


def test_the_panel_shows_the_code_and_the_repair_loop():
    body = HTML.split("function renderAnalysis()", 1)[1].split("\nfunction ", 1)[0]
    assert "The code that produced this" in body
    assert "attempt_log" in body and "fed back as a traceback" in body


def test_charts_are_rendered_as_images():
    body = HTML.split("function renderAnalysis()", 1)[1].split("\nfunction ", 1)[0]
    assert "_loadChart" in body
    assert "data:${d.content_type};base64" in HTML


def test_binary_artifacts_are_served_at_all():
    """read_text() raises on a PNG, so a chart could not be fetched before this."""
    server = Path("web/server.py").read_text()
    assert "UnicodeDecodeError" in server and "b64encode" in server


def test_the_artifacts_reach_the_screen():
    assert "if(u.analysis_artifacts) _pushArts(u.analysis_artifacts);" in HTML
    assert "if(u.analysis_report) RUN.analysis_rep=u.analysis_report;" in HTML


def test_the_playbook_describes_execution_not_summarisation():
    card = HTML.split("{id:'analysis',", 1)[1].split("]},", 1)[0]
    assert "tracks:['analysis']" in card
    assert "RUNS it" in card and "traceback" in card
    assert "'Executes it'" in card


def test_the_question_and_data_source_are_shown_not_folded():
    intake = HTML.split("  analysis:{fields:[", 1)[1].split("]},", 1)[0]
    for fid in ("question", "data_source"):
        line = next(ln for ln in intake.splitlines() if f"FIELD('{fid}'" in ln)
        assert line.rstrip().rstrip("),]}").endswith("true"), f"{fid} sits in the collapsed fold"


def test_the_journey_has_an_analysis_stage():
    assert "key:'analysis'" in HTML and "node:'analyze_data'" in HTML
    assert "analyze_data:'Data Analyst agent'" in HTML


def test_an_analysis_run_reports_lines_computed_not_tests_passed():
    assert "k: RUN.analysis_rep.ok ? 'Lines computed' : 'No answer'" in HTML


# ------------------------------------------------- it is not the other agent
def test_the_two_analysis_agents_are_distinct_tracks():
    from src.registry import build_manifest
    ids = {c["id"] for c in build_manifest()["columns"]}
    assert {"analysis", "insights"} <= ids


def test_the_insights_agent_still_computes_its_own_figures():
    """The mirror property: there the model must NOT do arithmetic."""
    from src.integration import analytics
    assert "call_llm" not in inspect.getsource(analytics)


if __name__ == "__main__":     # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
