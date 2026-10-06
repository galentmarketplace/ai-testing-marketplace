"""Gate policy, advisory checks, registry planning, and redaction — the logic customers' results rest on."""
from src.gates.quality_gates import _decide
from src.registry import BY_ID, plan_for
from web.server import _redact


def _state(**perf):
    run = {"run_id": "r", "suite": "feature", "passed": 10, "failed": 0, "failures": []}
    if perf:
        run["perf"] = {"p95_ms": 100, "error_rate": 0.0, **perf}
    return {"run_results": [run]}


def test_qg1_passes_within_policy():
    d = _decide(_state(), "QG1", "feature", "generate_ui_scripts")["gate_decisions"][-1]
    assert d["verdict"] == "pass"


def test_qg1_fails_on_p99_and_reports_margin():
    d = _decide(_state(p95_ms=700, p99_ms=9000), "QG1", "feature", "generate_ui_scripts")["gate_decisions"][-1]
    assert d["verdict"] == "fail"
    assert any("p99" in c["label"] and not c["ok"] for c in d["checks"])
    assert any("margin" in c["label"] for c in d["checks"])


def test_oracle_finding_fails_qg1():
    st = _state()
    st["oracle_findings"] = [{"case_id": "FC-1", "issue": "assertion removed"}]
    d = _decide(st, "QG1", "feature", "generate_ui_scripts")["gate_decisions"][-1]
    assert d["verdict"] == "fail" and any("FC-1" in c["label"] for c in d["checks"])


def test_advisory_pod_checks_never_fail_the_gate():
    st = _state(pod_resources={"connected": True, "pods": [
        {"pod": "api-1", "cpu_peak_pct": 97, "cpu_peak_m": 485, "cpu_limit_m": 500,
         "mem_peak_pct": 99, "mem_peak_mi": 507, "mem_limit_mi": 512, "restarts": 2}]})
    d = _decide(st, "QG1", "feature", "generate_ui_scripts")["gate_decisions"][-1]
    assert d["verdict"] == "pass", "resource saturation informs; regenerating the script can't fix it"
    assert [c for c in d["checks"] if c.get("advisory") and not c["ok"]]


def test_plan_orders_dependencies():
    ids = [s.id for s in plan_for({"mode": "custom", "tracks": ["criteria", "functional"], "open_pr": False})]
    assert ids.index("generate_ac") < ids.index("generate_functional_cases") < ids.index("generate_ui_scripts")
    assert ids.index("execute_scripts") < ids.index("oracle_check") < ids.index("quality_gate_1")


def test_security_gate_never_loops():
    assert BY_ID["security_gate"].on_fail_reset == () and BY_ID["coverage_gate"].on_fail_reset == ()


def test_redact_scrubs_secrets_at_every_depth():
    out = _redact({"config": {"jira": {"token": "ATATT-secret", "ticket": "QA-1"}},
                   "inputs": {"login_password": "pw", "base_url": "http://app"},
                   "list": [{"github_token": "ghp_x"}]})
    assert out["config"]["jira"]["token"] == "***" and out["config"]["jira"]["ticket"] == "QA-1"
    assert out["inputs"]["login_password"] == "***" and out["inputs"]["base_url"] == "http://app"
    assert out["list"][0]["github_token"] == "***"


# ---- skipped cases must be visible, never silently folded into a 100% pass ----
def _qg1(passed, failed, skipped, **policy_over):
    from src.config import GATE_POLICY
    from src.gates.quality_gates import quality_gate_1
    saved = dict(GATE_POLICY["QG1"])
    GATE_POLICY["QG1"].update(policy_over)
    try:
        st = {"run_results": [{"run_id": "r", "suite": "feature", "passed": passed,
                               "failed": failed, "skipped": skipped, "failures": []}]}
        return quality_gate_1(st)["gate_decisions"][-1]
    finally:
        GATE_POLICY["QG1"].clear()
        GATE_POLICY["QG1"].update(saved)


def test_skipped_cases_appear_as_their_own_criterion():
    d = _qg1(5, 0, 4)
    labels = " | ".join(c["label"] for c in d["checks"])
    assert "4 of 9 case(s) not executed" in labels
    assert "44%" in labels


def test_a_high_skip_rate_is_advisory_by_default_so_it_does_not_block():
    d = _qg1(5, 0, 4)
    assert d["verdict"] == "pass"
    skip_check = next(c for c in d["checks"] if "not executed" in c["label"])
    assert skip_check["ok"] is False and skip_check["advisory"] is True


def test_skips_can_be_made_blocking_by_policy():
    d = _qg1(5, 0, 4, skip_blocking=True)
    assert d["verdict"] == "fail", "with skip_blocking the unverified cases must stop the run"


def test_no_skips_adds_no_extra_criterion():
    d = _qg1(5, 0, 0)
    assert not any("not executed" in c["label"] for c in d["checks"])


def test_the_pass_rate_label_shows_the_real_counts():
    d = _qg1(5, 1, 2)
    assert "5 passed, 1 failed, 2 skipped" in d["checks"][0]["label"]


def test_skip_rate_is_computed_over_all_cases_not_just_executed():
    from src.state import RunResult
    r = RunResult(run_id="r", suite="feature", passed=5, failed=0, skipped=4)
    assert r.pass_rate == 1.0        # conventional: of those executed
    assert round(r.skip_rate, 3) == 0.444


# ---- the runner must never invent a passing test from no measurement ----
def test_a_run_with_nothing_measured_is_blocked_not_passed():
    """`passed or 1` used to report ONE pass when nothing ran, which the gate read as
    a 100% pass rate: a green verdict manufactured from no evidence."""
    from pathlib import Path

    from src.state import RunResult
    # Comments explaining the old bug mention it by name, so inspect CODE lines only.
    code = "\n".join(ln for ln in Path("src/runner/executor.py").read_text().splitlines()
                     if not ln.lstrip().startswith("#"))
    assert "passed=passed or 1" not in code, "the fabricated-pass fallback is back"
    assert "passed=passed," in code
    assert "produced no measurable result" in code

    # and the gate must fail such a result rather than pass it
    blocked = RunResult(run_id="r", suite="feature", passed=0, failed=1,
                        failures=[{"test": "feature runner", "error": "no measurable result"}])
    assert blocked.pass_rate == 0.0


def test_an_unparseable_report_counts_as_a_failure_not_a_pass():
    from pathlib import Path
    code = "\n".join(ln for ln in Path("src/runner/executor.py").read_text().splitlines()
                     if not ln.lstrip().startswith("#"))
    assert "could not parse the Playwright JSON report" in code
    assert "passed += 1 if proc.returncode == 0 else 0" not in code, \
        "the exit code is being trusted over actual evidence again"
