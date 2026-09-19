"""Jenkins Automation — a self-healing CI stage (drop-in marketplace plugin).

Three specs make one capability:
  * run_jenkins  — triggers the UI + regression job(s) against the PR branch and
                   records the result as a `jenkins` run.
  * jenkins_gate — a POST-PR gate: green => done; red => loop back to the fixer.
  * jenkins_fix  — heals the failing automation (UI: regenerate the spec from the
                   real DOM + the Jenkins error; regression: self-heal the cases)
                   and commits the fix onto the PR branch, so the next Jenkins run
                   picks it up. The orchestrator loops fix -> run -> gate until the
                   Jenkins run is GREEN and the PR is ready to merge.

The rule the user asked for: automation *code* only exists when the UI Agent is
involved. So run_jenkins runs only when there is UI automation OR a regression
suite to execute; otherwise it is a no-op that the gate treats as "nothing to
verify".

Connection (env or per-run inputs): JENKINS_URL, JENKINS_USER, JENKINS_TOKEN, and
the job name(s): JENKINS_UI_JOB (or JENKINS_JOB), optional JENKINS_REGRESSION_JOB.
"""
import os
from pathlib import Path

from ...integration import github, jenkins
from ...llm import call_llm_json
from ...registry import AgentSpec, DisplayNode
from ...state import GateDecision, PipelineState
from ..heal_agent import heal_regression
from ..ui_automation_agent import generate_ui_scripts


def _tracks(state: PipelineState) -> set:
    return set(state.get("run_config", {}).get("tracks", []))


def _has_automation(state: PipelineState) -> bool:
    """True only when the UI Agent produced automation code (Playwright specs)."""
    return any(a.get("type") == "playwright" for a in state.get("test_artifacts", []))


def _should_run(state: PipelineState) -> bool:
    return _has_automation(state) or ("regression" in _tracks(state))


def _host_url(u: str | None) -> str | None:
    """Jenkins runs in Docker — localhost there is the container, not the host. Rewrite so the
    job can reach an app running on the host machine."""
    if not u:
        return u
    return u.replace("//localhost", "//host.docker.internal").replace("//127.0.0.1", "//host.docker.internal")


def _is_full_name(s: str | None) -> bool:
    """owner/repo (a real GitHub repo), not a local path like 'repos/idurar' or a URL."""
    return bool(s) and s.count("/") == 1 and not s.startswith(("repos/", "http", "/", "."))


def _resolve_target(state: PipelineState, inp: dict):
    """(repo, branch) Jenkins should check out. A pushed branch (functional) wins. Only fall back to
    a repo's DEFAULT branch for regression (existing cases) — NOT when we have generated automation
    but the push failed, since running main would just fail on a missing framework. None => skip."""
    pr = state.get("pr", {}) or {}
    if pr.get("repo") and pr.get("branch"):
        return pr["repo"], pr["branch"]
    if _has_automation(state):
        return None, None          # generated specs but no branch = push failed → don't run main
    for cand in (inp.get("dest_repo"), inp.get("source_repo")):
        if _is_full_name(cand):
            return cand, None      # regression: run existing cases on the default branch
    return None, None


def _params(inp: dict, repo: str, branch: str | None) -> dict:
    return {
        "REPO": f"https://github.com/{repo}.git",
        "BRANCH": branch or "",
        "BASE_URL": _host_url(inp.get("base_url")) or "http://host.docker.internal:3000",
        # login is per-repo config; env is only the bundled-demo fallback
        "LOGIN_EMAIL": inp.get("login_user") or os.environ.get("APP_USER") or os.environ.get("IDURAR_USER", ""),
        "LOGIN_PASSWORD": inp.get("login_password") or os.environ.get("APP_PASS") or os.environ.get("IDURAR_PASS", ""),
    }


def _jenkins_run_result(passed: int, failed: int, failures: list, n: int) -> dict:
    return {"run_id": f"jenkins-{n}", "suite": "jenkins",
            "passed": passed, "failed": failed, "failures": failures}


def run_jenkins(state: PipelineState) -> dict:
    inp = state.get("story", {}).get("inputs", {}) or {}
    rr = list(state.get("run_results", []))
    attempts = dict(state.get("attempts", {}))
    attempts["run_jenkins"] = attempts.get("run_jenkins", 0) + 1

    if not _should_run(state):
        note = ("No automation to run — the UI Automation agent is not in this config and no "
                "regression suite is selected, so Jenkins has nothing to execute.")
        print(f"  [Jenkins] skipped: {note}")
        return {"jenkins_report": {"status": "skipped", "reason": note},
                "run_results": rr + [_jenkins_run_result(0, 0, [], attempts["run_jenkins"])],
                "attempts": attempts}

    # Mock mode: don't hit a real Jenkins — simulate a green CI run so demos complete cleanly.
    if os.environ.get("MOCK_LLM"):
        n = attempts["run_jenkins"]
        passed = 3 if n >= 2 else 2   # first attempt "red", self-heal, then green (shows the loop)
        failed = 0 if n >= 2 else 1
        fails = [] if failed == 0 else [{"test": "[ui-automation] login flow", "error": "expect(locator).toBeVisible() failed (mock)"}]
        overall = "SUCCESS" if failed == 0 else "FAILURE"
        job = os.environ.get("JENKINS_UI_JOB") or os.environ.get("JENKINS_JOB") or "ui-automation"
        base = (os.environ.get("JENKINS_URL") or "http://localhost:8081").rstrip("/")
        mock_run = {"label": "ui-automation", "job": job, "result": overall,
                    "url": f"{base}/job/{job}/", "mock": True,
                    "report": {"passed": passed, "failed": failed}}
        print(f"  [Jenkins] (mock) attempt {n}: {overall} ({passed}✓ {failed}✗)")
        return {"jenkins_report": {"status": "ran", "overall": overall, "passed": passed, "failed": failed,
                                   "runs": [mock_run], "mock": True},
                "run_results": rr + [_jenkins_run_result(passed, failed, fails, n)],
                "attempts": attempts}

    if not jenkins.configured():
        note = ("Jenkins not connected — set JENKINS_URL / JENKINS_USER / JENKINS_TOKEN and a job "
                "(JENKINS_UI_JOB, optionally JENKINS_REGRESSION_JOB).")
        print(f"  [Jenkins] {note}")
        return {"jenkins_report": {"status": "not_configured", "reason": note},
                "run_results": rr + [_jenkins_run_result(0, 0, [], attempts["run_jenkins"])],
                "attempts": attempts}

    # Jenkins needs a real GitHub repo/branch to check out. Without one (e.g. Destination = "Draft
    # only", or a local demo path) there is nothing to run AND nowhere for Self-Heal to push fixes —
    # so skip cleanly with a clear message instead of looping to a block.
    repo, branch = _resolve_target(state, inp)
    if not repo:
        note = ("No destination repository selected — pick a Destination repo (or 'Create a new "
                "repository') on the Configure screen so Jenkins can check out the tests and Self-Heal "
                "can push fixes to the branch. Nothing to run.")
        print(f"  [Jenkins] {note}")
        return {"jenkins_report": {"status": "skipped", "reason": note},
                "run_results": rr + [_jenkins_run_result(0, 0, [], attempts["run_jenkins"])],
                "attempts": attempts}

    ui_job = inp.get("jenkins_ui_job") or os.environ.get("JENKINS_UI_JOB") or os.environ.get("JENKINS_JOB")
    reg_job = inp.get("jenkins_regression_job") or os.environ.get("JENKINS_REGRESSION_JOB") or ui_job
    jobs = []
    if _has_automation(state) and ui_job:
        jobs.append(("ui-automation", ui_job, ""))
    if "regression" in _tracks(state) and reg_job:
        jobs.append(("regression", reg_job, inp.get("tags", "") or ""))

    params = _params(inp, repo, branch)
    runs, passed, failed, failures = [], 0, 0, []
    for label, job, grep in jobs:
        try:
            r = jenkins.run_job(job, {**params, "GREP": grep})
            r["label"] = label
            runs.append(r)
            rep = r.get("report") or {}
            passed += rep.get("passed", 0)
            failed += rep.get("failed", 0)
            for f in r.get("failures", []):
                failures.append({"test": f"[{label}] {f['test']}", "error": f["error"], "file": f.get("file", "")})
            # a build that failed to even run tests (e.g. checkout/compile) has no test failures
            if r.get("result") != "SUCCESS" and not r.get("failures"):
                failures.append({"test": f"[{label}] build {r.get('result')}",
                                 "error": (r.get("console", "") or "")[-600:]})
                failed += 1 if rep.get("failed", 0) == 0 else 0
            print(f"  [Jenkins] {label}: #{r['number']} {r['result']} "
                  f"({rep.get('passed', 0)}✓ {rep.get('failed', 0)}✗) {r['url']}")
        except Exception as exc:
            runs.append({"label": label, "job": job, "result": "ERROR", "error": str(exc)})
            failed += 1
            failures.append({"test": f"[{label}] job error", "error": str(exc)})
            print(f"  [Jenkins] {label} on '{job}' errored: {exc}")

    overall = "SUCCESS" if failed == 0 and all(r.get("result") == "SUCCESS" for r in runs if "result" in r) else "FAILURE"
    report = {"status": "ran", "overall": overall, "passed": passed, "failed": failed, "runs": runs}
    return {"jenkins_report": report,
            "run_results": rr + [_jenkins_run_result(passed, failed, failures, attempts["run_jenkins"])],
            "attempts": attempts}


def jenkins_gate(state: PipelineState) -> dict:
    rep = state.get("jenkins_report", {}) or {}
    status = rep.get("status")
    if status in (None, "skipped", "not_configured"):
        verdict, reason = "pass", f"Jenkins {status or 'not run'} — nothing to verify."
    else:
        failed = rep.get("failed", 0)
        ok = rep.get("overall") == "SUCCESS" and failed == 0
        verdict = "pass" if ok else "fail"
        reason = ("Jenkins is GREEN — all automation passed; PR is ready to merge."
                  if ok else f"Jenkins is RED — {failed} failing test(s); self-healing.")
    checks = [{"label": reason, "threshold": "Jenkins SUCCESS · 0 failures", "ok": verdict == "pass"}]
    decision = GateDecision(gate="JENKINS", verdict=verdict, checks=checks, reason=reason,
                            route_to="jenkins_fix" if verdict == "fail" else None)
    print(f"  [Gate JENKINS] {verdict.upper()} — {reason}")
    return {"gate_decisions": state.get("gate_decisions", []) + [decision.model_dump()]}


def _commit_fixes(state: PipelineState) -> list[str]:
    """Push the healed automation onto the PR branch so the next Jenkins run picks it up.
    No-op when there is no PR yet (e.g. a local pre-PR gate loop) — the fix stays in the
    workspace for the next local run."""
    pr = state.get("pr", {}) or {}
    if not (github.connected() and pr.get("repo") and pr.get("branch")):
        return []
    latest = {}   # newest content per spec filename (generate_ui_scripts overwrites in place)
    for a in state.get("test_artifacts", []):
        if a.get("type") == "playwright":
            latest[Path(a["path"]).name] = a["path"]
    committed = []
    for name, path in latest.items():
        try:
            github.put_file(pr["repo"], f"tests/{name}", Path(path).read_text(),
                            pr["branch"], f"self-heal: fix {name}")
            committed.append(name)
        except Exception as exc:
            print(f"  [Self-Heal] could not commit {name}: {exc}")
    return committed


_REG_SYSTEM = """You are a test-automation engineer repairing a failing Playwright/TypeScript test.
You are given the failing spec AND its imported local modules (page objects / helpers), plus the exact
CI failure. The defect is usually a stale/incorrect LOCATOR or ASSERTION in ONE of these files (often a
page object, not the spec). Fix ONLY that one file, using resilient Playwright locators
(getByRole/getByLabel/getByPlaceholder/getByText). Do NOT change test intent or weaken assertions just
to force a pass.
Respond with ONLY JSON: {"path": "<the repo-relative path of the ONE file you changed>", "content": "<full corrected file>"}"""


def _resolve_import(spec_path: str, imp: str) -> str:
    import posixpath
    return posixpath.normpath(posixpath.join(posixpath.dirname(spec_path), imp))


def _fetch_module(repo: str, base: str, branch: str):
    for cand in (base, base + ".ts", base + ".js", base + "/index.ts", base + "/index.js"):
        try:
            return cand, github.get_file(repo, cand, branch)
        except Exception:
            continue
    return None, None


def _heal_regression_branch(state: PipelineState) -> list[str]:
    """REAL regression heal: fetch the failing spec + its imported local modules (page objects),
    ask the LLM which file to repair, and commit the fix back to the PR branch."""
    import re
    pr = state.get("pr", {}) or {}
    repo, branch = pr.get("repo"), pr.get("branch")
    if not (github.connected() and repo and branch):
        return []
    jr = state.get("jenkins_report", {}) or {}
    fails = [f for run in jr.get("runs", []) for f in run.get("failures", []) if f.get("file")]
    if not fails:
        rr = next((r for r in reversed(state.get("run_results", [])) if r.get("suite") == "jenkins"), None)
        fails = [f for f in (rr or {}).get("failures", []) if f.get("file")]

    seen, committed = set(), []
    for f in fails:
        spec_path = f["file"].strip()
        if not spec_path.endswith((".ts", ".js")) or spec_path in seen:
            continue
        seen.add(spec_path)
        try:
            spec = github.get_file(repo, spec_path, branch)
        except Exception as exc:
            print(f"  [Self-Heal] couldn't fetch {spec_path}: {exc}"); continue
        # gather the spec + its RELATIVE imports (page objects/helpers) so the real defect is reachable
        files = {spec_path: spec}
        for imp in re.findall(r"""from\s+['"](\.\.?/[^'"]+)['"]""", spec):
            if len(files) >= 5:
                break
            p, c = _fetch_module(repo, _resolve_import(spec_path, imp), branch)
            if p and c and p not in files:
                files[p] = c
        ctx = (f"FAILING TEST: {f.get('test')}\nCI ERROR:\n{f.get('error')}\n\n"
               + "\n\n".join(f"=== FILE: {p} ===\n{c}" for p, c in files.items()))
        try:
            raw = call_llm_json("regression_agent", _REG_SYSTEM, ctx, max_tokens=6000)
            path = (raw.get("path") or "").strip()
            content = (raw.get("content") or "").strip()
        except Exception as exc:
            print(f"  [Self-Heal] LLM repair failed for {spec_path}: {exc}"); continue
        if path in files and content and content != files[path]:
            try:
                github.put_file(repo, path, content, branch, f"self-heal: repair {Path(path).name}")
                committed.append(path)
                print(f"  [Self-Heal] repaired + committed {path}")
            except Exception as exc:
                print(f"  [Self-Heal] couldn't commit {path}: {exc}")
        else:
            print(f"  [Self-Heal] LLM returned no usable fix for {spec_path} (path={path!r})")
    return committed


def self_heal(state: PipelineState) -> dict:
    """The ONE self-heal agent. Fired whenever a run fails — local regression (QG2) or a Jenkins run
    (JENKINS gate). It repairs whatever actually failed and commits to the PR branch so the next
    Jenkins run picks it up. UI: regenerate the spec from the real DOM + error. Regression: fetch the
    failing spec from the repo, LLM-repair the failing case, commit.
    """
    tracks = _tracks(state)
    pr = state.get("pr", {}) or {}
    has_branch = bool(pr.get("repo") and pr.get("branch"))
    last_fail = next((r for r in reversed(state.get("run_results", [])) if r.get("failed", 0) > 0), None)
    suite = last_fail["suite"] if last_fail else ""

    heal_ui = (_has_automation(state) or "functional" in tracks) and suite in ("feature", "jenkins", "")
    heal_reg = ("regression" in tracks) and suite in ("regression", "jenkins", "")

    updates, s, committed = {}, state, []
    if heal_ui:
        upd = generate_ui_scripts(s); s = {**s, **upd}; updates.update(upd)
        committed += _commit_fixes(s)
    if heal_reg:
        if has_branch:
            committed += _heal_regression_branch(s)   # real: repair the repo's failing spec on the branch
        else:
            upd = heal_regression(s); s = {**s, **upd}; updates.update(upd)   # local QG2 (no branch): stub notes
    if not (heal_ui or heal_reg):   # fallback
        if _has_automation(state) or "functional" in tracks:
            upd = generate_ui_scripts(s); s = {**s, **upd}; updates.update(upd); committed += _commit_fixes(s)

    where = f"committed {len(committed)} fix(es) to the branch" if committed else "no committable fix produced"
    print(f"  [Self-Heal] {'UI ' if heal_ui else ''}{'regression ' if heal_reg else ''}heal after "
          f"{suite or 'a'} failure; {where}")
    return {**updates, "ci_heal": {"committed": committed, "suite": suite}}


_J, _EVAL, _HEAL = "#e05c3e", "#d29922", "#f778ba"
SPECS = [
    AgentSpec(
        id="run_jenkins", label="Jenkins CI", kind="agent", fn=run_jenkins,
        tracks=("jenkins",), depends_on=("push_branch", "self_heal"), category="Automation",
        produces="run the UI + regression automation in Jenkins against the PR branch",
        display=[DisplayNode("jenkins", "Jenkins CI", "🏗️", _J, "AUTOMATION", "agent", column="jenkins")],
    ),
    AgentSpec(
        id="jenkins_gate", label="JENKINS Gate", kind="gate", fn=jenkins_gate,
        tracks=("jenkins",), depends_on=("run_jenkins",), gate_name="JENKINS", post_pr=True,
        on_fail_reset=("self_heal", "run_jenkins", "jenkins_gate"), max_retries=6, category="Automation",
        produces="verify the Jenkins run is green (loop until it is)",
        display=[DisplayNode("jenkins_gate", "JENKINS Gate", "✅", _EVAL, "EVALUATION", "gate", parent="jenkins")],
    ),
    # The single, unified Self-Heal — serves BOTH the local regression gate (QG2) and the
    # Jenkins gate. trigger_only: it never runs in the forward flow, only when a gate loops back.
    AgentSpec(
        id="self_heal", label="Self-Heal", kind="agent", fn=self_heal,
        tracks=("functional", "regression", "jenkins"), trigger_only=True, category="Quality",
        produces="repair the failing automation (UI specs / regression cases) and push it to the PR",
        display=[DisplayNode("heal", "Self-Heal", "🩹", _HEAL, "SELF-HEAL", "agent", column="heal")],
    ),
]
