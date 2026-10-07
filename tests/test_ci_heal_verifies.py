"""A CI repair must be verified locally before it is committed.

Observed live on run 68: the healer regenerated a spec, committed it unseen, waited for a
full Jenkins build to learn it was still broken, and repeated six times before the run
blocked. One of those regenerations produced a locator matching two elements — a strict-mode
violation a single local run would have caught in seconds.
"""
import pytest

from src.agents.plugins import jenkins_agent as ja


def _state(**over):
    s = {"pr": {"repo": "org/repo", "branch": "ai-tests/x"},
         "run_results": [{"suite": "jenkins", "passed": 8, "failed": 3, "failures": []}],
         "test_artifacts": [{"type": "playwright", "path": "/tmp/x.spec.ts"}],
         "run_config": {"tracks": ["functional", "jenkins"]}}
    s.update(over)
    return s


def test_a_repair_that_passes_locally_is_committed(monkeypatch):
    monkeypatch.setattr(ja, "generate_ui_scripts", lambda s: {})
    monkeypatch.setattr(ja, "execute_scripts",
                        lambda s: {"run_results": [{"suite": "feature", "passed": 9, "failed": 0}]})
    monkeypatch.setattr(ja, "_commit_fixes", lambda s: ["login.spec.ts"])
    out = ja.self_heal(_state())
    assert out["ci_heal"]["committed"] == ["login.spec.ts"]
    assert "0 failed" in out["ci_heal"]["local_verification"]


def test_a_repair_that_still_fails_locally_is_not_committed(monkeypatch):
    """This is the whole point: do not spend a CI build proving what a local run just showed."""
    committed = []
    monkeypatch.setattr(ja, "generate_ui_scripts", lambda s: {})
    monkeypatch.setattr(ja, "execute_scripts",
                        lambda s: {"run_results": [{"suite": "feature", "passed": 8, "failed": 3}]})
    monkeypatch.setattr(ja, "_commit_fixes", lambda s: committed.append(1) or ["x"])
    out = ja.self_heal(_state())
    assert committed == [], "a still-broken repair was pushed to the branch"
    assert out["ci_heal"]["committed"] == []
    assert "3 failed" in out["ci_heal"]["local_verification"]


def test_a_runner_failure_does_not_block_the_repair(monkeypatch):
    """A broken runner is not a verdict on the repair; failing closed would strand the run."""
    def boom(s):
        raise RuntimeError("playwright missing")
    monkeypatch.setattr(ja, "generate_ui_scripts", lambda s: {})
    monkeypatch.setattr(ja, "execute_scripts", boom)
    monkeypatch.setattr(ja, "_commit_fixes", lambda s: ["login.spec.ts"])
    out = ja.self_heal(_state())
    assert out["ci_heal"]["committed"] == ["login.spec.ts"]
    assert "unavailable" in out["ci_heal"]["local_verification"]


def test_the_verification_outcome_is_recorded_either_way(monkeypatch):
    monkeypatch.setattr(ja, "generate_ui_scripts", lambda s: {})
    monkeypatch.setattr(ja, "execute_scripts",
                        lambda s: {"run_results": [{"suite": "feature", "passed": 1, "failed": 1}]})
    monkeypatch.setattr(ja, "_commit_fixes", lambda s: ["x"])
    out = ja.self_heal(_state())
    assert out["ci_heal"]["local_verification"], "the run record must say what verification found"


if __name__ == "__main__":     # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
