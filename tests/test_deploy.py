"""Deploy step: URL precedence, strategy detection, skip rules and teardown.

The precedence rule is the one that matters most. If a run builds a feature, deploys it, and
then the automation still drives the OLD configured environment, every test passes while
verifying nothing about the change. That is the exact false-pass the platform exists to stop,
so it gets a test.
"""
import json
import socket

import pytest

from src import runctx
from src.agents import deploy_agent
from src.integration import deployer
from src.state import target_url


# --------------------------------------------------------------- URL precedence
def test_deployed_url_beats_the_configured_base_url():
    state = {"deployment": {"ok": True, "url": "http://127.0.0.1:45001"},
             "story": {"inputs": {"base_url": "https://stale.example.com"}}}
    assert target_url(state) == "http://127.0.0.1:45001"


def test_falls_back_to_configured_url_when_nothing_was_deployed():
    state = {"story": {"inputs": {"base_url": "https://www.saucedemo.com"}}}
    assert target_url(state) == "https://www.saucedemo.com"


def test_a_failed_deploy_does_not_hijack_the_target():
    state = {"deployment": {"ok": False, "error": "build failed", "url": None},
             "story": {"inputs": {"base_url": "https://www.saucedemo.com"}}}
    assert target_url(state) == "https://www.saucedemo.com"


def test_trailing_slashes_are_normalised():
    assert target_url({"deployment": {"ok": True, "url": "http://h:1/"}}) == "http://h:1"
    assert target_url({"story": {"inputs": {"base_url": "http://h:2/"}}}) == "http://h:2"


def test_default_when_nothing_is_configured():
    assert target_url({}, "http://localhost:9999") == "http://localhost:9999"


# --------------------------------------------------------------- skip rules
def test_deploy_is_skipped_when_the_run_built_nothing():
    """A test-only run targets an existing environment; rebuilding it buys nothing."""
    out = deploy_agent.deploy_app({"story": {"inputs": {}}})
    assert out["deployment"]["skipped"] is True
    assert out["deployment"]["ok"] is False


def test_explicit_deploy_no_wins_even_with_generated_code():
    out = deploy_agent.deploy_app({"story": {"inputs": {"deploy": "no"}},
                                   "code_artifacts": [{"path": "x.js"}]})
    assert out["deployment"]["skipped"] is True


def test_mock_run_never_builds_or_binds_a_port():
    # reset with the TOKEN — calling set_run_context(mock=False) would leave a context
    # installed and silently break every test that relies on the env fallback.
    tok = runctx.set_run_context(mock=True, run_id="deploy-unit-test")
    try:
        out = deploy_agent.deploy_app({"story": {"inputs": {"deploy": "yes"}},
                                       "code_artifacts": [{"path": "x.js"}]})
    finally:
        runctx.reset_run_context(tok)
    d = out["deployment"]
    assert d["ok"] is True and d.get("mock") is True
    # nothing may actually be listening on the advertised mock port
    with socket.socket() as s:
        assert s.connect_ex(("127.0.0.1", d["port"])) != 0


# --------------------------------------------------------------- strategy detection
def _repo(tmp_path, name, files: dict):
    d = tmp_path / name
    d.mkdir()
    for rel, content in files.items():
        p = d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    return d


def test_node_app_with_build_and_preview_is_detected(tmp_path):
    r = _repo(tmp_path, "vite", {"package.json": json.dumps(
        {"scripts": {"build": "vite build", "preview": "vite preview"}})})
    assert deployer.detect_strategy(r) == "node"


def test_a_prebuilt_directory_is_served_statically(tmp_path):
    r = _repo(tmp_path, "static", {"dist/index.html": "<h1>hi</h1>"})
    assert deployer.detect_strategy(r) == "static"


def test_a_repo_with_no_way_to_serve_reports_none(tmp_path):
    r = _repo(tmp_path, "bare", {"README.md": "nothing to run"})
    assert deployer.detect_strategy(r) == "none"


def test_unservable_repo_fails_loudly_rather_than_pretending(tmp_path):
    r = _repo(tmp_path, "bare2", {"README.md": "x"})
    res = deployer.deploy(r, run_id="t", strategy="auto")
    assert res["ok"] is False and "no way to serve" in res["error"]


def test_missing_repo_path_is_an_error_not_a_crash(tmp_path):
    res = deployer.deploy(tmp_path / "does-not-exist", run_id="t")
    assert res["ok"] is False and "not found" in res["error"]


def test_exposed_container_port_is_read_from_the_dockerfile(tmp_path):
    r = _repo(tmp_path, "dk", {"Dockerfile": "FROM nginx\nEXPOSE 8080/tcp\n"})
    assert deployer._container_port(r) == 8080
    r2 = _repo(tmp_path, "dk2", {"Dockerfile": "FROM nginx\n"})
    assert deployer._container_port(r2) == 80


# --------------------------------------------------------------- ports & teardown
def test_free_port_is_actually_bindable():
    p = deployer.free_port()
    with socket.socket() as s:
        s.bind(("127.0.0.1", p))          # would raise if the port were taken


def test_teardown_run_clears_every_handle_for_that_run():
    deployer._HANDLES["run-a"] = [{"kind": "proc", "proc": None, "port": 1},
                                  {"kind": "proc", "proc": None, "port": 2}]
    deployer._HANDLES["run-b"] = [{"kind": "proc", "proc": None, "port": 3}]
    assert deployer.teardown_run("run-a") == 2
    assert "run-a" not in deployer._HANDLES
    assert deployer.active("run-b") == [{"kind": "proc", "port": 3}]
    deployer.teardown_run("run-b")


def test_teardown_of_an_unknown_run_is_harmless():
    assert deployer.teardown_run("never-existed") == 0


# --------------------------------------------------------------- scheduling
def test_the_automation_agent_waits_for_the_deploy_when_one_is_planned():
    from src.registry import BY_ID, plan_for
    assert "deploy_app" in BY_ID["generate_ui_scripts"].depends_on
    ids = [s.id for s in plan_for({"mode": "full", "entry": "from_story",
                                   "tracks": ["build", "criteria", "functional"]})]
    assert ids.index("deploy_app") < ids.index("generate_ui_scripts")


def test_a_test_only_run_schedules_no_deploy_and_still_plans_the_automation():
    from src.registry import plan_for
    ids = [s.id for s in plan_for({"mode": "custom", "entry": "from_existing_code",
                                   "tracks": ["criteria", "functional"]})]
    assert "deploy_app" not in ids
    assert "generate_ui_scripts" in ids


def test_the_pr_opens_only_after_the_ci_gate():
    """Their flow: branch -> Jenkins -> CI green -> THEN open the PR for human approval."""
    from src.registry import BY_ID
    assert BY_ID["open_pr"].final is True
    assert BY_ID["jenkins_gate"].post_pr is True
    assert BY_ID["push_branch"].ship is True


if __name__ == "__main__":     # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
