"""A run must measure the REPOSITORY, not a tree the platform polluted itself.

Live defect: the Go test generator writes its proposals into the cached clone to compile
them, and left the verified ones behind. The next run measured 96.4% instead of the
repository's real 28.6%, concluded there was nothing to fix, generated no tests and opened
no pull request — reporting a state that existed on no branch. A demo run would have shown
a green gate and no work done.
"""
import subprocess

import pytest

from src.integration import repo_analyzer


def _repo(tmp_path):
    """A real git repo, cloned the way the platform clones one."""
    origin = tmp_path / "origin"
    origin.mkdir()
    def run(*a):
        subprocess.run(["git", *a], cwd=str(origin), check=True, capture_output=True)

    run("init", "-q", "-b", "main")
    run("config", "user.email", "t@example.com")
    run("config", "user.name", "t")
    (origin / "main.go").write_text("package main\n")
    run("add", "-A")
    run("commit", "-qm", "initial")
    return origin


def _clone(origin, tmp_path):
    dest = tmp_path / "repos" / "svc"
    dest.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "clone", "--depth", "1", str(origin), str(dest)],
                   check=True, capture_output=True)
    return dest


def test_residue_from_a_previous_run_is_removed(tmp_path):
    clone = _clone(_repo(tmp_path), tmp_path)
    residue = clone / "internal" / "pricing_generated_test.go"
    residue.parent.mkdir(parents=True, exist_ok=True)
    residue.write_text("package pricing\n")

    repo_analyzer._refresh_clone(clone)
    assert not residue.exists(), "a generated test survived into the next run's measurement"


def test_a_refreshed_clone_picks_up_new_commits(tmp_path):
    origin = _repo(tmp_path)
    clone = _clone(origin, tmp_path)
    assert not (clone / "second.go").exists()

    (origin / "second.go").write_text("package main\n")
    subprocess.run(["git", "add", "-A"], cwd=str(origin), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "second"], cwd=str(origin), check=True,
                   capture_output=True)

    repo_analyzer._refresh_clone(clone)
    assert (clone / "second.go").exists(), "the clone analysed a commit the repo has moved past"


def test_tracked_file_edits_are_discarded_too(tmp_path):
    clone = _clone(_repo(tmp_path), tmp_path)
    (clone / "main.go").write_text("package main // locally mangled\n")
    repo_analyzer._refresh_clone(clone)
    assert "mangled" not in (clone / "main.go").read_text()


def test_an_unreachable_remote_does_not_fail_the_run(tmp_path, capsys):
    clone = _clone(_repo(tmp_path), tmp_path)
    subprocess.run(["git", "-C", str(clone), "remote", "set-url", "origin",
                    str(tmp_path / "gone")], check=True, capture_output=True)
    repo_analyzer._refresh_clone(clone)                      # must not raise
    assert "could not fetch" in capsys.readouterr().out


def test_an_existing_clone_is_refreshed_not_reused_blindly(tmp_path, monkeypatch):
    monkeypatch.setattr(repo_analyzer, "REPOS_DIR", tmp_path / "repos")
    dest = tmp_path / "repos" / "svc"
    dest.mkdir(parents=True)
    seen = []
    monkeypatch.setattr(repo_analyzer, "_refresh_clone", lambda d: seen.append(d))
    out = repo_analyzer._ensure_local("https://github.com/org/svc.git")
    assert out == dest and seen == [dest], "an existing clone was handed back unrefreshed"


def test_a_local_path_is_never_reset(tmp_path):
    """Pointing the platform at your own working tree must not destroy uncommitted work."""
    origin = _repo(tmp_path)
    (origin / "work-in-progress.go").write_text("package main\n")
    out = repo_analyzer._ensure_local(str(origin))
    assert out == origin
    assert (origin / "work-in-progress.go").exists()


def test_the_generator_removes_what_it_placed_for_verification():
    """Source-level guard: cleanup cannot sit only on the failure branch."""
    import inspect

    from src.agents import go_test_agent
    src = inspect.getsource(go_test_agent.generate_go_tests)
    body = src[src.index("if t.returncode == 0:"):]
    # the unlink loop must be outside the else:, i.e. at the same indent as the if/else
    assert "\n        for pl in placed:\n            pl.unlink(missing_ok=True)" in body, \
        "placed files are only removed when verification FAILS — a passing run pollutes the clone"


if __name__ == "__main__":     # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
