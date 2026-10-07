"""Deploy agent — boots the app under test so the automation drives the REAL running feature.

Deterministic (no LLM). This is the step that makes "an agent wrote a feature" testable: the
Dev Agent's code is only worth anything once it is actually running. The agent builds the
application repository, serves it on a free loopback port, waits until it genuinely answers,
and publishes the URL into run state as `deployment.url`.

Everything downstream (Playwright agent, feature runner, accessibility, performance) reads
that URL in preference to the static `base_url` in the project config, so the suite targets
the build containing the new feature rather than whatever was configured months ago.

Inputs (all optional):
  deploy            "yes"|"no"        — force on/off; default: on when the build track ran
  deploy_strategy   auto|node|docker|static|none
  deploy_health     path polled for readiness (default "/")
  deploy_port       pin a port instead of taking a free one

Failure is honest: if the app does not boot, the agent reports ok=false with the build log
tail and the run falls back to the configured base_url rather than silently testing nothing.
"""
from .. import runctx, sandbox
from ..integration import deployer
from ..integration.repo_analyzer import _ensure_local
from ..state import PipelineState

_MOCK = {"ok": True, "url": "http://127.0.0.1:41234", "strategy": "node (mock)",
         "port": 41234, "log": "(mock run — nothing was built or started)", "mock": True}


def _repo_path(state: PipelineState) -> str | None:
    """The APPLICATION repo (what we deploy), not the test repo.

    The Dev Agent's per-run working copy wins: deploying the analyzer's pristine cache would
    boot the app WITHOUT the feature this run just implemented, and every test would then
    pass against code that does not contain the change.
    """
    if state.get("app_workdir"):
        return state["app_workdir"]
    a = state.get("repo_analysis") or {}
    if a.get("path"):
        return a["path"]
    inp = state.get("story", {}).get("inputs", {}) or {}
    src = inp.get("repo") or inp.get("source_repo")
    if not src:
        return None
    try:
        return str(_ensure_local(src))
    except Exception:
        return None


def _wanted(state: PipelineState, inp: dict) -> bool:
    flag = str(inp.get("deploy") or "").strip().lower()
    if flag in ("no", "false", "0", "off"):
        return False
    if flag in ("yes", "true", "1", "on"):
        return True
    # The deploy track selected on its own is an explicit request to deploy. Without this a
    # playbook that includes the track still skipped it, so the load test fell back to the
    # configured base_url — which for a saved Configuration can be a THIRD-PARTY site.
    if "deploy" in set((state.get("run_config") or {}).get("tracks") or []):
        return True
    # Otherwise: deploy when this run actually built something — a configured base_url
    # already points at a running app and rebuilding it buys nothing.
    return bool(state.get("code_artifacts"))


def deploy_app(state: PipelineState) -> dict:
    inp = state.get("story", {}).get("inputs", {}) or {}

    if not _wanted(state, inp):
        print("  [Deploy] no feature code in this run — keeping the configured app URL")
        return {"deployment": {"ok": False, "skipped": True,
                               "reason": "nothing was built in this run; using the configured base_url"}}

    if runctx.is_mock():
        print("  [Deploy] mock run — no build, no container, no port bound")
        return {"deployment": dict(_MOCK)}

    repo = _repo_path(state)
    if not repo:
        return {"deployment": {"ok": False, "skipped": True,
                               "reason": "no application repository resolved to deploy"}}

    run_id = sandbox.run_id_for_path(sandbox.run_workspace()) or ""
    strategy = (inp.get("deploy_strategy") or "auto").strip().lower()
    health = inp.get("deploy_health") or "/"
    port = inp.get("deploy_port")
    try:
        port = int(port) if port else None
    except (TypeError, ValueError):
        port = None

    print(f"  [Deploy] building and booting {repo} (strategy={strategy}) …")
    res = deployer.deploy(repo, run_id=run_id, strategy=strategy, port=port, health_path=health)

    if res.get("ok"):
        print(f"  [Deploy] live at {res['url']}  (via {res['strategy']})")
    else:
        print(f"  [Deploy] FAILED — {res.get('error', 'unknown error')}")
    return {"deployment": res}


def deployment_url(state: PipelineState) -> str | None:
    """The URL downstream agents should target, or None to fall back to configured base_url."""
    d = state.get("deployment") or {}
    return d.get("url") if d.get("ok") else None
