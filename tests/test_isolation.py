"""Phase 0.3/0.9 acceptance: customer code never sees platform secrets; runs never share artifacts."""
import os

from src import runctx, sandbox


def test_no_platform_secret_reaches_a_child(monkeypatch):
    for k, v in {"ANTHROPIC_API_KEY": "sk-ant-LEAK", "GITHUB_CLIENT_SECRET": "ghs-LEAK",
                 "GITHUB_TOKEN": "ghp-LEAK", "JENKINS_TOKEN": "jk-LEAK", "ATM_API_TOKEN": "atm-LEAK",
                 "ATM_SECRET_KEY": "fernet-LEAK"}.items():
        monkeypatch.setenv(k, v)
    env = sandbox.child_env({"BASE_URL": "http://app", "LOGIN_PASSWORD": "app-pw"})
    assert not [k for k, v in env.items() if "LEAK" in str(v)]
    assert env["BASE_URL"] == "http://app" and env["LOGIN_PASSWORD"] == "app-pw"   # explicit values pass
    assert env.get("CI") == "1"


def test_inherit_refuses_secret_looking_names(monkeypatch):
    monkeypatch.setenv("MY_API_KEY", "nope")
    monkeypatch.setenv("BUILD_FLAVOUR", "release")
    env = sandbox.child_env(inherit=("MY_API_KEY", "BUILD_FLAVOUR"))
    assert "MY_API_KEY" not in env and env["BUILD_FLAVOUR"] == "release"


def test_path_is_preserved_so_toolchains_still_run():
    assert sandbox.child_env().get("PATH") == os.environ.get("PATH")


def test_two_runs_do_not_share_artifact_paths():
    paths = []
    for rid in ("iso-A", "iso-B"):
        tok = runctx.set_run_context(mock=True, run_id=rid)
        p = sandbox.run_workspace("security") / "security-report.md"
        p.write_text(f"findings for {rid}")
        paths.append(p)
        runctx.reset_run_context(tok)
    a, b = paths
    assert a != b and a.read_text() != b.read_text()
    assert sandbox.run_id_for_path(a) == "iso-A" and sandbox.run_id_for_path(b) == "iso-B"


def test_no_run_context_uses_the_legacy_shared_dir():
    assert sandbox.run_id_for_path(sandbox.run_workspace("perf") / "x.json") is None
