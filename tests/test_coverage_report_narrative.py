"""The report at the END of a coverage run must describe the fix, not only the gap.

Two live defects this pins, both on the `go-orders-service` run that genuinely raised
coverage 28.6% → 96.4% with two verified test files:

  * the PR body said "No new test files were added as part of this PR" and "Existing test
    suite already satisfies the required coverage threshold" — the exact opposite of the
    diff, written by a model that was never shown the diff
  * `coverage-report.md` showed only the 28.6% before state, so the artifact a reviewer
    opens made a successful run look like a failed one
"""
import pytest

from src.agents import go_coverage_agent, pr_agent
from src.integration import go_coverage as gocov

BEFORE = {
    "ok": True, "tests_passed": True, "total_pct": 28.6, "min_pct": 80.0,
    "statements": 28, "statements_covered": 8,
    "files": [{"file": "internal/cart/cart.go", "lines": 14, "covered": 2, "pct": 14.3},
              {"file": "internal/pricing/pricing.go", "lines": 14, "covered": 6, "pct": 42.9}],
    "funcs": [{"file": "internal/cart/cart.go", "line": 20, "func": "Count", "pct": 0.0}],
    "uncovered_funcs": [
        {"file": "internal/cart/cart.go", "line": 20, "func": "Count", "pct": 0.0},
        {"file": "internal/cart/cart.go", "line": 28, "func": "Remove", "pct": 0.0},
        {"file": "internal/pricing/pricing.go", "line": 30, "func": "BulkDiscount", "pct": 0.0},
        {"file": "internal/pricing/pricing.go", "line": 41, "func": "LoyaltyPoints", "pct": 0.0}],
}
AFTER = {
    "ok": True, "tests_passed": True, "total_pct": 96.4,
    "statements": 28, "statements_covered": 27,
    "files": [{"file": "internal/cart/cart.go", "lines": 14, "covered": 13, "pct": 92.9},
              {"file": "internal/pricing/pricing.go", "lines": 14, "covered": 14, "pct": 100.0}],
    "funcs": [{"file": "internal/cart/cart.go", "line": 20, "func": "Count", "pct": 100.0},
              {"file": "internal/cart/cart.go", "line": 28, "func": "Remove", "pct": 100.0},
              {"file": "internal/pricing/pricing.go", "line": 30, "func": "BulkDiscount", "pct": 100.0},
              {"file": "internal/pricing/pricing.go", "line": 41, "func": "LoyaltyPoints", "pct": 100.0}],
    "uncovered_funcs": [],
}
PROPOSED = {"proposed": 2, "verified": True, "note": "compiled & passed in the cloned repo",
            "covers": ["BulkDiscount", "Count", "LoyaltyPoints", "Remove"], "skipped": []}
FIXED = {**BEFORE, "after": AFTER, "total_pct_after": 96.4, "proposed_tests": PROPOSED}

STATE = {"story": {"inputs": {"repo": "galentmarketplace/go-orders-service", "min_coverage": "80"}},
         "coverage_report": FIXED,
         "coverage_artifacts": [
             {"type": "coverage-tests", "path": "/tmp/a", "repo_path": "internal/cart/cart_generated_test.go"},
             {"type": "coverage-tests", "path": "/tmp/b", "repo_path": "internal/pricing/pricing_generated_test.go"}],
         "test_artifacts": []}


# ------------------------------------------------------------------ the artifact
def test_the_report_shows_before_and_after():
    md = gocov.report_markdown(FIXED, "go-orders-service", 80.0)
    assert "28.6% → 96.4%" in md
    assert "+67.8 points" in md


def test_the_report_names_the_functions_that_moved():
    md = gocov.report_markdown(FIXED, "go-orders-service", 80.0)
    for fn in ("Count", "Remove", "BulkDiscount", "LoyaltyPoints"):
        assert f"`{fn}`" in md, f"{fn} went from 0% to covered and the report does not say so"
    assert "## What this run changed" in md


def test_the_report_does_not_still_list_fixed_functions_as_uncovered():
    md = gocov.report_markdown(FIXED, "go-orders-service", 80.0)
    tail = md.split("## Coverage by file", 1)[1]
    assert "candidates for new tests" not in tail
    assert "None" in tail.split("Still uncovered", 1)[1]


def test_the_report_uses_the_post_fix_per_file_numbers():
    md = gocov.report_markdown(FIXED, "go-orders-service", 80.0)
    assert "92.9%" in md and "14.3%" not in md


def test_a_measure_only_run_still_reports_the_gap():
    """No tests generated → the report is a gap report, exactly as before."""
    md = gocov.report_markdown(BEFORE, "go-orders-service", 80.0)
    assert "Total coverage: 28.6%" in md
    assert "candidates for new tests" in md
    assert "What this run changed" not in md


def test_unverified_proposals_are_never_shown_as_a_coverage_gain():
    rep = {**BEFORE, "proposed_tests": {**PROPOSED, "verified": False,
                                        "note": "unverified — `go` not installed"}}
    md = gocov.report_markdown(rep, "go-orders-service", 80.0)
    assert "96.4" not in md
    assert "unverified" in md


# ------------------------------------------------------------------ the PR narrative
def test_the_pr_body_states_what_the_diff_contains():
    d = pr_agent._draft(STATE)
    assert "2 generated unit test file(s)" in d["body"]
    assert "cart_generated_test.go" in d["body"]
    assert "28.6%" in d["body"] and "96.4%" in d["body"]


def test_the_pr_title_describes_the_coverage_change():
    assert pr_agent._draft(STATE)["title"] == "Raise test coverage 28.6% → 96.4%"


@pytest.mark.parametrize("lie", [
    "no new test files",
    "already satisfies",
    "no new tests were needed",
    "no acceptance criteria were specified",
])
def test_the_pr_body_cannot_claim_nothing_was_added(lie):
    body = pr_agent._draft(STATE)["body"].lower()
    assert lie not in body


def test_the_coverage_draft_is_deterministic_not_model_written(monkeypatch):
    """The model is not consulted for a coverage fix — it cannot see the diff."""
    monkeypatch.setattr(pr_agent, "call_llm_json",
                        lambda *a, **k: pytest.fail("the model was asked to describe the diff"))
    pr_agent._draft(STATE)


def test_the_body_says_the_figure_was_measured():
    assert "re-measured" in pr_agent._draft(STATE)["body"]


def test_an_unverified_coverage_run_says_so_in_the_pr():
    state = {**STATE, "coverage_report": {**BEFORE, "proposed_tests": {
        **PROPOSED, "verified": False, "note": "unverified — `go` not installed"}}}
    body = pr_agent._draft(state)["body"]
    assert "Not execution-verified" in body


def test_a_run_that_generated_nothing_falls_back_to_the_model(monkeypatch):
    called = {}

    def fake(agent, system, user, **kw):
        called["user"] = user
        return {"title": "t", "body": "b"}

    monkeypatch.setattr(pr_agent, "call_llm_json", fake)
    pr_agent._draft({"story": {"inputs": {}}, "coverage_report": BEFORE,
                     "test_artifacts": [{"path": "/tmp/login.spec.ts"}]})
    assert "login.spec.ts" in called["user"], "the drafter was not shown the diff"


# ------------------------------------------------------------------ the gate
def test_the_gate_counts_uncovered_from_the_same_measurement():
    """'96.4% — 4 uncovered function(s)' named exactly the four it had just covered."""
    out = go_coverage_agent.coverage_gate(
        {"coverage_report": FIXED, "story": {"inputs": {}}, "gate_decisions": []})
    d = out["gate_decisions"][-1]
    assert "96.4%" in d["reason"]
    assert "0 uncovered function(s) remaining" in d["reason"]
    assert "raised from 28.6%" in d["reason"]


def test_the_gate_still_reports_real_remaining_gaps():
    rep = {**FIXED, "after": {**AFTER, "total_pct": 72.0,
                              "uncovered_funcs": [{"file": "a.go", "line": 1, "func": "X"}]},
           "total_pct_after": 72.0}
    d = go_coverage_agent.coverage_gate(
        {"coverage_report": rep, "story": {"inputs": {}}, "gate_decisions": []})["gate_decisions"][-1]
    assert "1 uncovered function(s) remaining" in d["reason"]


if __name__ == "__main__":     # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))


# ---- a generator that produces nothing must say so, not look like it never tried ----
def test_a_failed_generation_is_visible_in_the_report():
    rep = {**BEFORE, "proposed_tests": {"proposed": 0, "covers": [], "skipped": [],
                                        "verified": False,
                                        "note": "no usable test file was generated (0 entries)"}}
    md = gocov.report_markdown(rep, "go-orders-service", 80.0)
    assert "nothing usable produced" in md
    assert "failed attempt, not a decision" in md
    assert "candidates for new tests" in md          # the gap is still listed


def test_a_failed_generation_never_claims_a_coverage_gain():
    rep = {**BEFORE, "proposed_tests": {"proposed": 0, "covers": [], "skipped": [],
                                        "verified": False, "note": "x"}}
    md = gocov.report_markdown(rep, "go-orders-service", 80.0)
    assert "→" not in md.split("## Coverage by file")[0].replace("—", "")
    assert "Total coverage: 28.6%" in md


def test_a_failed_generation_does_not_produce_a_coverage_pr_narrative():
    """With no files to ship there is nothing to describe — fall back, don't invent."""
    state = {**STATE, "coverage_artifacts": [],
             "coverage_report": {**BEFORE, "proposed_tests": {"proposed": 0, "covers": [],
                                                              "skipped": [], "verified": False,
                                                              "note": "x"}}}
    assert pr_agent._coverage_facts(state) is None


# ---- the covered-function list must be measured, not taken from the model's word ----
def test_the_covered_functions_come_from_the_measurement():
    """Live: the model omitted `covers`, so a run that covered four functions reported
    'for 0 function(s)' and the PR said it covered none."""
    rep = {**FIXED, "proposed_tests": {**PROPOSED, "covers": []}}
    fixed = gocov.functions_fixed(rep)
    assert sorted(f["func"] for f in fixed) == ["BulkDiscount", "Count", "LoyaltyPoints", "Remove"]

    md = gocov.report_markdown(rep, "go-orders-service", 80.0)
    assert "for 4 previously-uncovered function(s)" in md

    d = pr_agent._draft({**STATE, "coverage_report": rep})
    assert "covering 4 function(s)" in d["body"]
    assert "`BulkDiscount`" in d["body"]


def test_a_function_still_at_zero_is_not_reported_as_fixed():
    after = {**AFTER, "total_pct": 60.0,
             "funcs": [{"file": "internal/cart/cart.go", "line": 20, "func": "Count", "pct": 100.0},
                       {"file": "internal/cart/cart.go", "line": 28, "func": "Remove", "pct": 0.0}],
             "uncovered_funcs": [{"file": "internal/cart/cart.go", "line": 28, "func": "Remove"}]}
    fixed = gocov.functions_fixed({**FIXED, "after": after})
    assert [f["func"] for f in fixed] == ["Count"]


def test_nothing_is_claimed_fixed_without_a_post_fix_measurement():
    assert gocov.functions_fixed(BEFORE) == []
    assert gocov.functions_fixed({**BEFORE, "proposed_tests": PROPOSED}) == []
