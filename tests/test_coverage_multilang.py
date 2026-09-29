"""Multi-language coverage: LCOV parsing, language detection, and honest scope.

The subtle failure this guards against is a coverage tool that only instruments files its
tests happen to import. It then reports 100% while ignoring every untested file, which is
the most flattering possible lie: technically true, completely useless. The runner reports
how much of the codebase was even looked at, so the headline number cannot stand alone.
"""
import json

import pytest

from src.integration import coverage, reports

LCOV = """TN:
SF:/repo/src/a.js
DA:1,3
DA:2,0
DA:3,1
end_of_record
SF:/repo/src/b.js
DA:1,0
DA:2,0
end_of_record
"""


# ------------------------------------------------------------------ LCOV parsing
def test_lcov_totals_and_per_file_percentages():
    r = reports.parse_lcov(LCOV, root="/repo")
    assert r["statements"] == 5 and r["statements_covered"] == 2
    assert r["total_pct"] == 40.0
    by = {f["file"]: f for f in r["files"]}
    assert by["src/a.js"]["pct"] == 66.7
    assert by["src/b.js"]["pct"] == 0.0


def test_files_are_ordered_worst_first():
    """The reader wants the gaps, not the wins."""
    r = reports.parse_lcov(LCOV, root="/repo")
    assert r["files"][0]["file"] == "src/b.js"


def test_repeated_line_records_take_the_highest_hit_count():
    """Merged reports emit the same line twice; the line is covered if any run hit it."""
    r = reports.parse_lcov("SF:/r/x.js\nDA:1,0\nDA:1,4\nend_of_record\n", root="/r")
    assert r["total_pct"] == 100.0


def test_float_hit_counts_are_tolerated():
    r = reports.parse_lcov("SF:/r/x.js\nDA:1,2.0\nend_of_record\n", root="/r")
    assert r["statements_covered"] == 1


def test_an_empty_report_does_not_divide_by_zero():
    r = reports.parse_lcov("", root="/r")
    assert r["total_pct"] == 0.0 and r["statements"] == 0


# ------------------------------------------------------------------ detection
def _repo(tmp_path, name, files):
    d = tmp_path / name
    for rel, body in files.items():
        p = d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)
    return d


def test_a_go_module_is_detected(tmp_path):
    r = _repo(tmp_path, "g", {"go.mod": "module x", "main.go": "package main"})
    assert coverage.detect_language(r) == "go"


def test_a_jest_project_is_detected(tmp_path):
    r = _repo(tmp_path, "n", {"package.json": json.dumps({"devDependencies": {"jest": "^29"}})})
    assert coverage.detect_language(r) == "node"


def test_a_python_project_is_detected(tmp_path):
    r = _repo(tmp_path, "p", {"pyproject.toml": "[project]", "app.py": "x = 1"})
    assert coverage.detect_language(r) == "python"


def test_a_cached_third_party_repo_does_not_make_the_workspace_look_like_go(tmp_path):
    """An unbounded search finds go.mod inside a cloned dependency and mislabels everything."""
    r = _repo(tmp_path, "ws", {"pyproject.toml": "[project]",
                               "repos/somelib/go.mod": "module somelib",
                               "node_modules/dep/go.mod": "module dep"})
    assert coverage.detect_language(r) == "python"


def test_an_unrecognised_repo_says_so_rather_than_reporting_zero(tmp_path):
    r = _repo(tmp_path, "u", {"README.md": "nothing here"})
    assert coverage.detect_language(r) == "unknown"
    out = coverage.measure(r)
    assert out["ok"] is False and out["not_applicable"] is True


def test_a_missing_path_is_an_error_not_a_crash(tmp_path):
    out = coverage.measure(tmp_path / "nope")
    assert out["ok"] is False and "not found" in out["error"]


# ------------------------------------------------------------------ honest scope
def test_a_hundred_percent_over_half_the_files_is_reported_as_such(tmp_path):
    r = _repo(tmp_path, "s", {"src/a.js": "x", "src/b.js": "x", "src/c.js": "x", "src/d.js": "x"})
    rep = coverage.annotate_scope(
        {"ok": True, "total_pct": 100.0, "files": [{"file": "src/a.js"}, {"file": "src/b.js"}]},
        r, "node")
    assert rep["files_in_repo"] == 4 and rep["files_measured"] == 2
    assert rep["scope_pct"] == 50.0
    assert rep["effective_pct"] == 50.0, "the number a reviewer should trust"
    assert "not instrumented" in rep["scope_warning"]


def test_full_instrumentation_produces_no_warning(tmp_path):
    r = _repo(tmp_path, "f", {"src/a.js": "x"})
    rep = coverage.annotate_scope({"ok": True, "total_pct": 80.0, "files": [{"file": "src/a.js"}]},
                                  r, "node")
    assert rep["scope_pct"] == 100.0 and "scope_warning" not in rep


def test_package_qualified_paths_still_match_the_source_walk(tmp_path):
    """Go reports github.com/org/repo/mux.go; the walk yields mux.go. These are one file."""
    r = _repo(tmp_path, "g2", {"go.mod": "module github.com/org/repo", "mux.go": "package mux"})
    rep = coverage.annotate_scope(
        {"ok": True, "total_pct": 90.0, "files": [{"file": "github.com/org/repo/mux.go"}]},
        r, "go")
    assert rep["scope_pct"] == 100.0, "suffix matching failed, so scope read as 0%"


def test_tests_mocks_and_build_config_are_not_counted_as_source(tmp_path):
    r = _repo(tmp_path, "x", {
        "src/a.js": "x",
        "src/a.test.js": "t", "src/__mocks__/m.js": "m",
        "jest.config.js": "c", "vite.config.js": "c",
        "coverage/lcov-report/block.js": "generated",
        "node_modules/dep/index.js": "dep"})
    rep = coverage.annotate_scope({"ok": True, "total_pct": 100.0, "files": [{"file": "src/a.js"}]},
                                  r, "node")
    assert rep["files_in_repo"] == 1, f"counted non-source files: {rep.get('unmeasured_sample')}"
    assert rep["scope_pct"] == 100.0


def test_a_failed_measurement_is_not_annotated(tmp_path):
    rep = coverage.annotate_scope({"ok": False, "error": "boom"}, tmp_path, "node")
    assert "scope_pct" not in rep


if __name__ == "__main__":     # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
