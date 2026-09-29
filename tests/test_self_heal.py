"""Self-heal: triage before repair, and never claim a repair that did not happen.

Two properties are load-bearing.

  1. A product defect must NEVER be healed. Rewriting a test until it passes converts a
     caught bug into a silent regression, which is the opposite of what the platform is for.
  2. The agent must not report a repair unless it actually wrote a file. The previous version
     appended "repaired N cases" while touching nothing, so the run history lied and the
     retry loop burned attempts re-running identical tests.
"""
import pytest

from src import runctx
from src.agents import heal_agent


def _state(failures, spec_path=None, suite="regression"):
    s = {"run_results": [{"suite": suite, "passed": 1, "failed": len(failures),
                          "failures": failures}],
         "acceptance_criteria": {"criteria": [{"id": "AC-1", "gherkin": "user can log in"}]}}
    if spec_path:
        s["test_artifacts"] = [{"type": "playwright", "path": str(spec_path)}]
    return s


def _spec(tmp_path, body="import {test} from '@playwright/test';\ntest('login', async () => {});\n"):
    p = tmp_path / "login.spec.ts"
    p.write_text(body)
    return p


def _triage(monkeypatch, classification, reason="r", evidence="e", repair=None):
    """Stub both LLM calls: triage first, then (optionally) repair."""
    calls = []

    def fake(agent, system, user, **kw):
        calls.append(agent)
        if agent.endswith("triage"):
            return {"verdicts": [{"test": "login", "classification": classification,
                                  "reason": reason, "evidence": evidence}]}
        return repair if repair is not None else {"changes": ["fixed locator"], "content": "REPAIRED\n"}

    monkeypatch.setattr(heal_agent, "call_llm_json", fake)
    return calls


# ------------------------------------------------- the rule that protects real bugs
def test_a_product_defect_is_never_healed_and_the_spec_is_untouched(tmp_path, monkeypatch):
    spec = _spec(tmp_path)
    original = spec.read_text()
    _triage(monkeypatch, "product_defect", reason="app shows no error for bad credentials")

    out = heal_agent.heal_regression(_state([{"test": "login", "error": "expected error banner"}], spec))

    assert spec.read_text() == original, "a real product bug was edited away"
    note = out["heal_notes"][-1]
    assert note["healed"] == []
    assert note["classification"] == "product_defect"
    esc = out["heal_escalations"]
    assert len(esc) == 1 and esc[0]["kind"] == "product_defect"
    assert "no error for bad credentials" in esc[0]["reason"]


def test_the_repair_model_is_never_even_called_for_a_product_defect(tmp_path, monkeypatch):
    spec = _spec(tmp_path)
    calls = _triage(monkeypatch, "product_defect")
    heal_agent.heal_regression(_state([{"test": "login", "error": "boom"}], spec))
    assert calls == ["heal_agent_triage"], "the repair step must not run after a product verdict"


def test_infrastructure_failures_are_escalated_not_rewritten(tmp_path, monkeypatch):
    spec = _spec(tmp_path)
    original = spec.read_text()
    _triage(monkeypatch, "infrastructure", reason="app was unreachable")
    out = heal_agent.heal_regression(_state([{"test": "login", "error": "ECONNREFUSED"}], spec))
    assert spec.read_text() == original
    assert out["heal_notes"][-1]["healed"] == []
    assert out["heal_escalations"][0]["kind"] == "infrastructure"


# ------------------------------------------------- the repair path
def test_a_test_defect_actually_rewrites_the_spec_and_reports_it(tmp_path, monkeypatch):
    spec = _spec(tmp_path)
    _triage(monkeypatch, "test_defect", reason="stale locator")
    out = heal_agent.heal_regression(_state([{"test": "login", "error": "locator not found"}], spec))

    assert spec.read_text() == "REPAIRED\n", "a test defect should have been repaired"
    note = out["heal_notes"][-1]
    assert note["healed"] == ["login"]
    assert note["classification"] == "test_defect"
    assert note["file"] == str(spec)


# ------------------------------------------------- never claim what did not happen
def test_no_repair_is_claimed_when_the_model_returns_nothing_usable(tmp_path, monkeypatch):
    spec = _spec(tmp_path)
    original = spec.read_text()
    _triage(monkeypatch, "test_defect", repair={"changes": [], "content": ""})
    out = heal_agent.heal_regression(_state([{"test": "login", "error": "x"}], spec))
    assert spec.read_text() == original
    assert out["heal_notes"][-1]["healed"] == []


def test_no_repair_is_claimed_when_there_is_no_spec_file(tmp_path, monkeypatch):
    _triage(monkeypatch, "test_defect")
    out = heal_agent.heal_regression(_state([{"test": "login", "error": "x"}], spec_path=None))
    note = out["heal_notes"][-1]
    assert note["healed"] == [] and "no spec file" in note["action"]


def test_a_failed_triage_refuses_to_rewrite_blind(tmp_path, monkeypatch):
    spec = _spec(tmp_path)
    original = spec.read_text()

    def boom(*a, **k):
        raise RuntimeError("model unavailable")
    monkeypatch.setattr(heal_agent, "call_llm_json", boom)

    out = heal_agent.heal_regression(_state([{"test": "login", "error": "x"}], spec))
    assert spec.read_text() == original
    assert out["heal_notes"][-1]["healed"] == []
    assert "triage failed" in out["heal_notes"][-1]["action"]


def test_no_failures_means_no_claim():
    out = heal_agent.heal_regression({"run_results": []})
    assert out["heal_notes"][-1]["healed"] == []
    assert "nothing" in out["heal_notes"][-1]["action"]


def test_a_mock_run_rewrites_nothing_and_says_so(tmp_path, monkeypatch):
    spec = _spec(tmp_path)
    original = spec.read_text()
    tok = runctx.set_run_context(mock=True, run_id="heal-test")
    try:
        out = heal_agent.heal_regression(_state([{"test": "login", "error": "x"}], spec))
    finally:
        runctx.reset_run_context(tok)
    assert spec.read_text() == original
    assert out["heal_notes"][-1]["mock"] is True
    assert out["heal_notes"][-1]["healed"] == []


# ------------------------------------------------- bookkeeping
def test_attempts_increment_so_the_orchestrator_can_bound_the_loop(tmp_path, monkeypatch):
    spec = _spec(tmp_path)
    _triage(monkeypatch, "test_defect")
    st = _state([{"test": "login", "error": "x"}], spec)
    out1 = heal_agent.heal_regression(st)
    st["attempts"] = out1["attempts"]
    out2 = heal_agent.heal_regression(st)
    assert out1["attempts"]["heal_regression"] == 1
    assert out2["attempts"]["heal_regression"] == 2


def test_pydantic_failure_objects_are_accepted_not_just_dicts(tmp_path, monkeypatch):
    from src.state import Failure
    spec = _spec(tmp_path)
    _triage(monkeypatch, "test_defect")
    st = _state([], spec)
    st["run_results"] = [{"suite": "regression", "passed": 0, "failed": 1,
                          "failures": [Failure(test="login", error="locator not found")]}]
    out = heal_agent.heal_regression(st)
    assert out["heal_notes"][-1]["healed"] == ["login"]


# ------------------------------------------------- orchestrator behaviour
def test_a_product_defect_blocks_before_a_retry_is_burned():
    """Retrying cannot fix a broken app, so the run must block immediately."""
    from src.orchestrator import product_defects
    state = {"heal_escalations": [
        {"kind": "product_defect", "test": "login", "reason": "no error banner"},
        {"kind": "infrastructure", "test": "cart", "reason": "app unreachable"}]}
    prod = product_defects(state)
    assert [e["test"] for e in prod] == ["login"]


def test_infrastructure_and_test_defects_do_not_short_circuit_the_retries():
    from src.orchestrator import product_defects
    assert product_defects({"heal_escalations": [{"kind": "infrastructure"}]}) == []
    assert product_defects({"heal_escalations": []}) == []
    assert product_defects({}) == []


if __name__ == "__main__":     # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
