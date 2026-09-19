"""Unit Test agent — runs a repo's REAL unit suite and reports.

Two modes:
  * existing repo (the standalone "Unit Tests" flow) → detect the repo's test framework,
    run its actual tests, write a report, and gate ADVISORY (report results; never loop —
    there's no dev agent to auto-fix someone else's code). "No tests" is a clear outcome,
    not an endless failure.
  * full build (entry=from_story + build track) → delegate to the strict quality gate so
    the Dev agent's self-heal loop still works for code we generated.
"""

from ..config import GENERATED_DIR
from ..gates import quality_gates
from ..integration import unit_runner
from ..integration.repo_analyzer import _ensure_local
from ..runner.executor import run_suite
from ..state import GateDecision, PipelineState


def _repo_path(state: PipelineState):
    a = state.get("repo_analysis") or {}
    if a.get("path"):
        return a["path"]
    src = (state.get("story", {}).get("inputs", {}) or {}).get("repo")
    if not src:
        return None
    try:
        return str(_ensure_local(src))
    except Exception:
        return None


def _building(state: PipelineState) -> bool:
    cfg = state.get("run_config", {}) or {}
    return cfg.get("entry") == "from_story" and "build" in (cfg.get("tracks") or [])


def run_unit_tests(state: PipelineState) -> dict:
    # Full-build mode keeps the original synthetic runner (Dev agent + self-heal own this).
    if _building(state) or not _repo_path(state):
        return run_suite("unit", state)

    path = _repo_path(state)
    repo = (state.get("story", {}).get("inputs", {}) or {}).get("source_repo") \
        or (state.get("story", {}).get("inputs", {}) or {}).get("repo") or "repo"
    print(f"  [Unit Agent] running the repo's unit tests at {path} …")
    r = unit_runner.run_unit(path)

    report_arts = list(state.get("unit_artifacts", []))
    md = unit_runner.report_markdown(r, repo)
    out = GENERATED_DIR / "unit" / "unit-report.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(md)
    report_arts.append({"type": "unit-report", "path": str(out), "tags": ["@unit"]})

    passed = r.get("passed", 0)
    failed = r.get("failed", 0)
    if r.get("no_tests"):
        print(f"  [Unit Agent] no tests to run — {r.get('message')}")
    elif r.get("ok"):
        print(f"  [Unit Agent] {r.get('framework')}: {passed} passed, {failed} failed")
    else:
        print(f"  [Unit Agent] could not run: {r.get('error')}")

    rr = list(state.get("run_results", []))
    rr.append({"run_id": "unit", "suite": "unit", "passed": passed, "failed": failed,
               "failures": r.get("failures", [])})
    return {"run_results": rr, "unit_report": r, "unit_artifacts": report_arts}


def unit_gate(state: PipelineState) -> dict:
    # Full-build mode uses the strict gate (drives the Dev agent's self-heal loop).
    if _building(state):
        return quality_gates.unit_gate(state)

    r = state.get("unit_report", {}) or {}
    if not r.get("ok"):
        verdict = "pass"   # advisory — couldn't run is reported, not looped
        reason = f"Unit tests could not run: {r.get('error', 'unknown')}."
        checks = [{"label": reason, "threshold": "informational", "ok": False}]
    elif r.get("no_tests"):
        verdict = "pass"
        reason = r.get("message", "No unit tests found in the repo.")
        checks = [{"label": "no unit tests in the repo", "threshold": "informational", "ok": True}]
    else:
        passed, failed = r.get("passed", 0), r.get("failed", 0)
        verdict = "pass"   # existing-repo results are REPORTED, not blocking (no dev agent to fix)
        reason = f"Ran the repo's unit suite ({r.get('framework')}): {passed} passed, {failed} failed."
        checks = [{"label": f"{passed} passed", "threshold": "—", "ok": True},
                  {"label": f"{failed} failed", "threshold": "0 ideal", "ok": failed == 0}]
    decision = GateDecision(gate="UNIT", verdict=verdict, reason=reason, checks=checks, route_to=None)
    print(f"  [Gate UNIT] {verdict.upper()} — {reason}")
    return {"gate_decisions": state.get("gate_decisions", []) + [decision.model_dump()]}
