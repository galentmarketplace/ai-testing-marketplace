"""Dev Agent working copy: isolation, write confinement, and what gets deployed.

Two failure modes this guards against, both of which produce a confident green run that
proves nothing:

  1. The agent edits the analyzer's SHARED clone. One run's half-finished feature then leaks
     into every later run's analysis, deploy and tests.
  2. Deploy boots the pristine cache instead of the edited copy, so the suite passes against
     code that does not contain the change.
"""
import subprocess

import pytest

from src.agents import deploy_agent, dev_agent
from src.integration import workspace


def _git_repo(tmp_path, name="app"):
    d = tmp_path / name
    (d / "src").mkdir(parents=True)
    (d / "src" / "app.js").write_text("export const v = 1;\n")
    (d / "package.json").write_text('{"scripts":{"build":"x","preview":"y"}}')
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t", "PATH": "/usr/bin:/bin:/usr/local/bin"}
    for args in (["init", "-q"], ["add", "-A"], ["commit", "-qm", "init"]):
        subprocess.run(["git", *args], cwd=str(d), env=env, capture_output=True, check=True)
    return d


# --------------------------------------------------------------- isolation
def test_a_worktree_is_created_and_the_shared_clone_is_untouched(tmp_path, monkeypatch):
    monkeypatch.setenv("ATM_GENERATED_DIR", str(tmp_path / "gen"))
    repo = _git_repo(tmp_path)
    ws = workspace.app_workdir(repo, run_id="r1")
    assert ws["ok"] and ws["mode"] == "worktree"

    from pathlib import Path
    wd = Path(ws["path"])
    (wd / "src" / "feature.js").write_text("export const f = 2;\n")

    assert workspace.changed_files(wd) == ["src/feature.js"]
    assert not (repo / "src" / "feature.js").exists(), "the edit leaked into the shared clone"

    assert workspace.cleanup_run("r1") == 1
    assert not wd.exists()


def test_a_non_git_directory_falls_back_to_a_copy(tmp_path, monkeypatch):
    monkeypatch.setenv("ATM_GENERATED_DIR", str(tmp_path / "gen"))
    plain = tmp_path / "plain"
    (plain / "src").mkdir(parents=True)
    (plain / "src" / "a.js").write_text("1")
    ws = workspace.app_workdir(plain, run_id="r2")
    assert ws["ok"] and ws["mode"] == "copy"
    from pathlib import Path
    assert (Path(ws["path"]) / "src" / "a.js").is_file()


def test_missing_repo_reports_an_error(tmp_path):
    ws = workspace.app_workdir(tmp_path / "nope", run_id="r3")
    assert ws["ok"] is False and "not found" in ws["error"]


def test_cleanup_of_an_unknown_run_is_harmless():
    assert workspace.cleanup_run("never-existed") == 0


def test_build_output_is_not_reported_as_a_source_change(tmp_path, monkeypatch):
    """node_modules and dist churn must not show up as the agent's edits."""
    monkeypatch.setenv("ATM_GENERATED_DIR", str(tmp_path / "gen"))
    repo = _git_repo(tmp_path, "app2")
    ws = workspace.app_workdir(repo, run_id="r4")
    from pathlib import Path
    wd = Path(ws["path"])
    (wd / "node_modules").mkdir(exist_ok=True)
    (wd / "node_modules" / "junk.js").write_text("x")
    (wd / "dist").mkdir(exist_ok=True)
    (wd / "dist" / "bundle.js").write_text("x")
    (wd / "src" / "real.js").write_text("x")
    assert workspace.changed_files(wd) == ["src/real.js"]
    workspace.cleanup_run("r4")


# --------------------------------------------------------------- write confinement
@pytest.mark.parametrize("bad", [
    "/etc/passwd",                      # absolute
    "../../../../etc/passwd",           # traversal
    "src/../../outside.js",             # traversal mid-path
    "~/secrets.txt",                    # home expansion
    "",                                 # empty
])
def test_unsafe_paths_are_refused(tmp_path, bad):
    assert dev_agent._safe_target(tmp_path, bad) is None


@pytest.mark.parametrize("good", ["src/app.js", "a/b/c/deep.jsx", "index.html"])
def test_repo_relative_paths_are_allowed(tmp_path, good):
    t = dev_agent._safe_target(tmp_path, good)
    assert t is not None and str(t).startswith(str(tmp_path.resolve()))


# --------------------------------------------------------------- what gets deployed
def test_deploy_prefers_the_edited_working_copy_over_the_pristine_cache():
    state = {"app_workdir": "/runs/abc/app/myapp",
             "repo_analysis": {"path": "/repos/myapp"}}
    assert deploy_agent._repo_path(state) == "/runs/abc/app/myapp"


def test_deploy_uses_the_analyzed_repo_when_nothing_was_edited():
    assert deploy_agent._repo_path({"repo_analysis": {"path": "/repos/myapp"}}) == "/repos/myapp"


# --------------------------------------------------------------- agent behaviour without an LLM
def test_no_repository_means_no_feature_and_an_explicit_note():
    out = dev_agent.generate_code({"acceptance_criteria": {"criteria": []},
                                   "story": {"inputs": {}}})
    assert out["code_artifacts"] == []
    assert "no application repository" in out["dev_note"]
    assert out["attempts"]["generate_code"] == 1


def test_relevance_ranking_prefers_implementation_over_existing_tests():
    files = ["src/cart.js", "src/__tests__/cart.test.js", "src/unrelated.js"]
    ranked = dev_agent._relevant(None, files, "the cart should total the items")
    assert ranked and ranked[0] == "src/cart.js"


if __name__ == "__main__":     # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
