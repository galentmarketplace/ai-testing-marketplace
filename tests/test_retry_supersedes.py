"""A retry's specs REPLACE the previous attempt's — they do not pile up.

Live defect (run #121): QG1 took four attempts, the Playwright agent wrote a new spec file
each time, and all of them were kept in `test_artifacts`. Delivery pushes every artifact, so
the branch carried three generated specs plus one already on main. The local Feature Runner
executes only the newest spec and went green; Jenkins checks out the BRANCH and ran the union
— 29 tests, 3 failing — so the run blocked. The CI healer then burned seven cycles trying to
repair tests from attempts this run had itself rejected.

Any run needing more than one attempt was guaranteed to pass locally and fail in CI.
"""
from pathlib import Path

import pytest

SRC = Path("src/agents/ui_automation_agent.py").read_text()


def test_previous_playwright_specs_are_dropped_before_the_new_ones_are_added():
    assert 'artifacts = [a for a in state.get("test_artifacts", []) if a.get("type") != "playwright"]' in SRC


def test_the_agent_no_longer_appends_to_the_previous_attempts_specs():
    assert 'artifacts = list(state.get("test_artifacts", []))' not in SRC, \
        "specs from a rejected attempt survive into delivery"


def test_artifacts_of_other_kinds_survive_a_retry():
    """Only the Playwright specs are superseded — coverage, security and k6 artifacts are not."""
    state = {"test_artifacts": [
        {"type": "playwright", "path": "/a/old.spec.ts"},
        {"type": "k6", "path": "/a/load.k6.js"},
        {"type": "web-vitals", "path": "/a/vitals.json"}]}
    kept = [a for a in state["test_artifacts"] if a.get("type") != "playwright"]
    assert [a["type"] for a in kept] == ["k6", "web-vitals"]


def test_the_replacement_is_announced():
    assert "a retry supersedes" in SRC


def test_delivery_ships_every_artifact_it_is_given():
    """Why the fix belongs in the agent: delivery has no way to know which spec is current."""
    pr = Path("src/agents/pr_agent.py").read_text()
    assert 'for art in state.get("test_artifacts", []):' in pr


if __name__ == "__main__":     # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
