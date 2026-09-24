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
