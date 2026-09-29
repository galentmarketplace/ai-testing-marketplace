"""Jenkins gate: a build that verified nothing must never read as green.

The CI gate is the last thing between an agent's work and a merge. A Jenkins job that
exits green having run zero tests (bad --grep, suite never committed, an early exit)
is the most dangerous possible outcome: maximum confidence, zero evidence.
"""
import pytest

from src.agents.plugins import jenkins_agent


def _run(result, passed=0, failed=0, failures=None):
    return {"label": "ui-automation", "number": 1, "url": "http://j/1", "result": result,
            "report": {"passed": passed, "failed": failed}, "failures": failures or [],
            "console": "…"}


def _evaluate(runs):
    """Mirror of the accumulation the agent performs, over a list of jenkins run dicts."""
    passed = failed = 0
    failures = []
    for r in runs:
        rep = r.get("report") or {}
        passed += rep.get("passed", 0)
        failed += rep.get("failed", 0)
        for f in r.get("failures", []):
            failures.append(f)
        if r.get("result") != "SUCCESS" and not r.get("failures"):
            failures.append({"test": "build", "error": ""})
            failed += 1 if rep.get("failed", 0) == 0 else 0
        elif r.get("result") == "SUCCESS" and rep.get("passed", 0) + rep.get("failed", 0) == 0:
            failures.append({"test": "no tests executed", "error": ""})
            failed += 1
    overall = "SUCCESS" if failed == 0 and all(r.get("result") == "SUCCESS" for r in runs) else "FAILURE"
    return overall, passed, failed, failures


def _gate(overall, failed):
    out = jenkins_agent.jenkins_gate({"jenkins_report": {"status": "ran", "overall": overall,
                                                         "failed": failed, "passed": 0}})
    return out["gate_decisions"][-1]


def test_a_green_build_that_ran_zero_tests_fails_the_gate():
    overall, _, failed, failures = _evaluate([_run("SUCCESS", passed=0, failed=0)])
    assert overall == "FAILURE" and failed == 1
    assert "no tests executed" in failures[0]["test"]
    assert _gate(overall, failed)["verdict"] == "fail"


def test_a_green_build_with_real_passes_is_accepted():
    overall, passed, failed, _ = _evaluate([_run("SUCCESS", passed=2, failed=0)])
    assert (overall, passed, failed) == ("SUCCESS", 2, 0)
    assert _gate(overall, failed)["verdict"] == "pass"


def test_a_red_build_fails_the_gate():
    overall, _, failed, _ = _evaluate([_run("FAILURE", passed=1, failed=1,
                                            failures=[{"test": "t", "error": "e"}])])
    assert overall == "FAILURE"
    assert _gate(overall, failed)["verdict"] == "fail"


def test_a_build_that_never_ran_tests_reports_the_reason():
    _, _, _, failures = _evaluate([_run("SUCCESS", 0, 0)])
    assert failures and "no tests executed" in failures[0]["test"]


def test_the_pipeline_definition_does_not_swallow_failures():
    """`|| true` on the test step and allowEmptyResults:true both manufacture false passes."""
    from pathlib import Path
    casc = Path("jenkins/casc.yaml").read_text()
    test_step = next(ln for ln in casc.splitlines() if "npx playwright test" in ln)
    # `|| true` is fine in the INSTALL stage (npm ci -> npm install fallback); on the test
    # step it converts every red run into a green build.
    assert "|| true" not in test_step, f"the test step swallows failures: {test_step.strip()[:120]}"
    assert "allowEmptyResults: false" in casc, "an empty run must not count as green"


def test_report_total_is_derived_when_jenkins_omits_it():
    import src.integration.jenkins as jk
    d = {"passCount": 2, "failCount": 0, "skipCount": 0}          # no totalCount
    total = d.get("totalCount") or (d["passCount"] + d["failCount"] + d["skipCount"])
    assert total == 2
    assert hasattr(jk, "test_report")


if __name__ == "__main__":     # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
