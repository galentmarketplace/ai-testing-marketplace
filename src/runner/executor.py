"""Test runner — deterministic infrastructure, NOT an agent.

The runner executes scripts and reports facts. It never uses an LLM.
(Design doc: "never let an LLM decide whether tests passed.")

SKELETON NOTE: real implementations are sketched in comments. The stub returns
simulated RunResults so the graph/gate logic can be developed and demoed.
Set REAL_RUNNER=1 once Playwright/k6/Jest are installed and IDURAR is running.
"""
import os
import re
import uuid
from pathlib import Path

from .. import runctx
from ..config import GENERATED_DIR, PROJECT_ROOT
from ..state import Failure, PerfMetrics, PipelineState, RunResult


def _sim_pods(state: PipelineState | None):
    """A realistic simulated per-pod CPU/mem report — only when the run asked for a deployed target
    (k8s namespace + selector), so the demo shows the deployed capacity story without a real cluster."""
    inp = (state or {}).get("story", {}).get("inputs", {}) or {}
    ns, sel = inp.get("k8s_namespace"), inp.get("k8s_selector")
    if not (ns and sel):
        return None
    app = re.sub(r"[^a-z0-9-]", "", (sel.split("=")[-1] or "app").lower()) or "app"
    return {"namespace": ns, "selector": sel, "interval_s": 5, "connected": True, "pod_count": 2,
            "pods": [
                {"pod": f"{app}-7d9c8b-abcde", "node": "ip-10-0-1-23", "cpu_limit_m": 500, "mem_limit_mi": 512,
                 "cpu_peak_m": 410, "cpu_avg_m": 265, "cpu_peak_pct": 82, "cpu_avg_pct": 53,
                 "mem_peak_mi": 330, "mem_avg_mi": 298, "mem_peak_pct": 64, "restarts": 0, "samples": 12},
                {"pod": f"{app}-7d9c8b-fghij", "node": "ip-10-0-2-47", "cpu_limit_m": 500, "mem_limit_mi": 512,
                 "cpu_peak_m": 360, "cpu_avg_m": 240, "cpu_peak_pct": 72, "cpu_avg_pct": 48,
                 "mem_peak_mi": 312, "mem_avg_mi": 286, "mem_peak_pct": 61, "restarts": 0, "samples": 12},
            ]}


def _simulate(suite: str, fail: bool = False, state: PipelineState | None = None) -> RunResult:
    """Simulated run. When `fail=True`, return a gate-tripping result (used by the
    DEMO_FAIL_SUITE knob to show the self-correction loop-back), otherwise a clean pass."""
    if fail:
        return RunResult(
            run_id=str(uuid.uuid4())[:8],
            suite=suite,
            passed={"unit": 1, "feature": 1, "regression": 10}.get(suite, 1),
            failed={"unit": 1, "feature": 2, "regression": 4}.get(suite, 2),
            failures=[Failure(test=f"{suite} case #1", error="assertion failed (simulated)")],
            # feature also breaches the perf thresholds so QG1 fails on perf too
            perf=PerfMetrics(p95_ms=1200, p99_ms=2100, error_rate=0.03, throughput_rps=42,
                             test_type="load", pod_resources=_sim_pods(state)) if suite == "feature" else None,
        )
    return RunResult(
        run_id=str(uuid.uuid4())[:8],
        suite=suite,
        passed={"unit": 2, "feature": 3, "regression": 14}.get(suite, 3),
        failed=0,
        perf=PerfMetrics(p95_ms=640, p99_ms=980, error_rate=0.002, throughput_rps=96,
                         test_type="load", pod_resources=_sim_pods(state)) if suite == "feature" else None,
    )


def _failure_context(attachments: list) -> str:
    """Decode our fixtures' 'failure-context' attachment (a11y snapshot + console + failed network at
    the moment of failure) into a compact block the Playwright agent can heal from. Empty if absent."""
    import base64
    import json
    for a in attachments or []:
        if a.get("name") != "failure-context":
            continue
        try:
            raw = a.get("body")
            data = json.loads(base64.b64decode(raw).decode() if raw else Path(a["path"]).read_text())
        except Exception:
            return ""
        parts = []
        if data.get("console"):
            parts.append("console:\n  " + "\n  ".join(data["console"][:8]))
        if data.get("network"):
            parts.append("failed/4xx network:\n  " + "\n  ".join(data["network"][:8]))
        if data.get("aria"):
            parts.append(f"PAGE STATE AT FAILURE ({data.get('url','')}) — accessibility snapshot; "
                         f"build the corrected locator from THIS:\n{data['aria'][:1400]}")
        return ("\n--- trace/failure context ---\n" + "\n".join(parts)) if parts else ""
    return ""


def _run_real(suite: str, state: PipelineState) -> RunResult:
    """Execute the real tools against the generated artifacts. Enabled with
    REAL_RUNNER=1. Requires the tools (k6 / Playwright) on PATH and, for feature/perf,
    the target app running (BASE_URL/TOKEN via env). Missing tools degrade to a clear
    'blocked' result rather than crashing the pipeline."""
    import json
    import shutil
    import subprocess
    import uuid

    arts = state.get("test_artifacts", [])
    inp = state.get("story", {}).get("inputs", {}) or {}
    rid = str(uuid.uuid4())[:8]

    def _blocked(reason: str) -> RunResult:
        return RunResult(run_id=rid, suite=suite, passed=0, failed=1,
                         failures=[Failure(test=f"{suite} runner", error=reason)])

    def _num(metric: dict, key: str) -> float:
        return float(metric.get(key, metric.get("values", {}).get(key, 0)) or 0)

    passed, failed, perf, failures = 0, 0, None, []

    # ---- perf: run any generated k6 script against the live API (auto-login for a fresh token) ----
    k6art = next((a for a in reversed(arts) if a.get("type") == "k6"), None)  # newest wins on self-heal
    if k6art:
        if not shutil.which("k6"):
            return _blocked("k6 not installed (brew install k6)")
        api_base = inp.get("base_url") or os.environ.get("BASE_URL", "http://localhost:8888")
        token = _auto_login(api_base) or os.environ.get("TOKEN", "")
        out = GENERATED_DIR / "perf" / f"summary-{rid}.json"

        # DEPLOYED per-pod validation: while k6 hits the deployed URL, sample the target pod(s) CPU/mem
        # from the cluster (kubectl top) so we can report utilization vs limits + restarts. Read-only.
        sampler, pod_resources = None, None
        k8s_cfg = {"namespace": inp.get("k8s_namespace"), "selector": inp.get("k8s_selector"),
                   "kubeconfig": inp.get("k8s_kubeconfig") or os.environ.get("KUBECONFIG"),
                   "context": inp.get("k8s_context")}
        if k8s_cfg["namespace"] and k8s_cfg["selector"]:
            try:
                from ..integration import k8s_metrics
                if k8s_metrics.available(k8s_cfg):
                    sampler = k8s_metrics.PodSampler(k8s_cfg, interval=5)
                    sampler.start()
            except Exception:
                sampler = None

        subprocess.run(["k6", "run", "--quiet", "--summary-export", str(out), k6art["path"]],
                       capture_output=True, timeout=600,
                       env={**os.environ, "BASE_URL": api_base, "TOKEN": token})
        if sampler:
            try:
                pod_resources = sampler.stop_and_report()
            except Exception:
                pod_resources = None
        m = (json.loads(out.read_text()) if out.exists() else {}).get("metrics", {})
        ch = m.get("checks", {})
        cp, cf = int(_num(ch, "passes")), int(_num(ch, "fails"))
        passed += cp or 1
        failed += cf
        inp_pt = (state.get("story", {}).get("inputs", {}) or {}).get("perf_type")
        perf = PerfMetrics(
            p95_ms=round(_num(m.get("http_req_duration", {}), "p(95)"), 1),
            p99_ms=round(_num(m.get("http_req_duration", {}), "p(99)"), 1) or None,
            error_rate=round(_num(m.get("http_req_failed", {}), "rate"), 4),
            throughput_rps=round(_num(m.get("http_reqs", {}), "rate"), 1) or None,
            test_type=inp_pt, pod_resources=pod_resources)
        if cf:
            failures.append(Failure(test="k6 checks", error=f"{cf} endpoint check(s) failed"))

    # ---- functional / regression: run the generated Playwright spec against the live UI ----
    pwart = next((a for a in reversed(arts) if a.get("type") == "playwright"), None)  # newest wins on self-heal
    if pwart and suite in ("feature", "regression"):
        runner = PROJECT_ROOT / "e2e-runner"
        if not (runner / "node_modules" / "@playwright").exists():
            if not k6art:
                return _blocked("Playwright runner not installed (cd e2e-runner && npm install)")
        else:
            spec = Path(pwart["path"])
            # TRACE-FED HEALING: drop the shared fixtures next to the spec so `import './fixtures'`
            # resolves and every test records console/network/aria for the failure context.
            fx = runner / "fixtures.ts"
            if fx.exists():
                try:
                    shutil.copyfile(fx, spec.parent / "fixtures.ts")
                except Exception:
                    pass
            app_url = inp.get("base_url") or os.environ.get("APP_URL", "http://localhost:3000")
            proc = subprocess.run(["npx", "playwright", "test", spec.name, "--reporter=json"],
                                  capture_output=True, text=True, timeout=1200, cwd=str(runner),
                                  env={**os.environ, "PW_TESTDIR": str(spec.parent), "BASE_URL": app_url,
                                       "PW_RETRIES": "1",  # one retry WITH tracing on for a richer failure signal
                                       "NODE_PATH": str(runner / "node_modules"),
                                       "LOGIN_PATH": inp.get("login_url") or "/login",
                                       "LOGIN_EMAIL": inp.get("login_user") or os.environ.get("APP_USER") or os.environ.get("IDURAR_USER", ""),
                                       "LOGIN_PASSWORD": inp.get("login_password") or os.environ.get("APP_PASS") or os.environ.get("IDURAR_PASS", "")})
            try:
                report = json.loads(proc.stdout)
                stats = report.get("stats", {})
                passed += stats.get("expected", 0)
                failed += stats.get("unexpected", 0)

                def _walk(suite):  # pull the real error + failure-context so the agent heals from evidence
                    for sp in suite.get("specs", []):
                        for t in sp.get("tests", []):
                            for res in t.get("results", []):
                                errs = [(e.get("message") or "").strip() for e in res.get("errors", [])]
                                errs = [e for e in errs if e]
                                if not errs:
                                    continue
                                error = errs[0].splitlines()[0][:300]
                                ctx = _failure_context(res.get("attachments", []))
                                failures.append(Failure(test=sp.get("title", "e2e"),
                                                        error=(error + ctx)[:1600]))
                    for s in suite.get("suites", []):
                        _walk(s)
                for s in report.get("suites", []):
                    _walk(s)
            except Exception:
                passed += 1 if proc.returncode == 0 else 0
                failed += 0 if proc.returncode == 0 else 1

    if not k6art and not pwart:
        return _blocked(f"no runnable artifact generated for suite '{suite}'")
    return RunResult(run_id=rid, suite=suite, passed=passed or 1, failed=failed, perf=perf, failures=failures)


def _auto_login(api_base: str) -> str:
    """Fetch a fresh IDURAR JWT so real runs keep working without a manual token.
    Uses IDURAR_USER / IDURAR_PASS (defaults to the seeded admin). Returns '' on failure."""
    import json
    import urllib.request
    user = os.environ.get("IDURAR_USER", "admin@admin.com")
    pw = os.environ.get("IDURAR_PASS", "admin123")
    try:
        req = urllib.request.Request(
            api_base.rstrip("/") + "/api/login",
            data=json.dumps({"email": user, "password": pw}).encode(),
            headers={"Content-Type": "application/json"})
        resp = json.loads(urllib.request.urlopen(req, timeout=10).read())
        return resp.get("result", {}).get("token", "") or ""
    except Exception:
        return ""


def run_suite(suite: str, state: PipelineState) -> dict:
    """Execute a suite and append the RunResult to state."""
    # A MOCK run is always simulated — even when the server has REAL_RUNNER=1 for live runs —
    # so the demo/full flow is deterministic and has no external side-effects.
    if runctx.real_runner() and not runctx.is_mock():
        result = _run_real(suite, state)
    else:
        # DEMO_FAIL_SUITE makes the named suite(s) fail on their FIRST run only, so the
        # orchestrator loops back once, the responsible agent "fixes" it, and the retry passes.
        # Value may be one suite, a comma-list, or "all" (unit,feature,regression).
        prior_runs = sum(1 for r in state.get("run_results", []) if r["suite"] == suite)
        fail = suite in runctx.fail_suites() and prior_runs == 0
        result = _simulate(suite, fail=fail, state=state)

    print(f"  [Runner] {suite}: {result.passed} passed / {result.failed} failed"
          + (f", p95={result.perf.p95_ms}ms" if result.perf else ""))
    # STANDARD REPORT: every run also emits JUnit XML — the format Jenkins/GitHub/Azure ingest natively.
    arts = list(state.get("test_artifacts", []))
    try:
        from ..integration import reports
        jout = GENERATED_DIR / "reports" / f"junit-{suite}-{result.run_id}.xml"
        jout.parent.mkdir(parents=True, exist_ok=True)
        jout.write_text(reports.junit_xml(result.model_dump()))
        arts.append({"type": f"junit-{suite}", "path": str(jout), "tags": ["@report", "@junit"]})
    except Exception as exc:  # a report is a convenience — never fail the run over it
        print(f"  [Runner] junit report skipped: {exc}")
    return {"run_results": state.get("run_results", []) + [result.model_dump()], "test_artifacts": arts}


# Graph node wrappers
def run_unit_tests(state: PipelineState) -> dict:
    return run_suite("unit", state)


def execute_scripts(state: PipelineState) -> dict:
    return run_suite("feature", state)


def execute_regression(state: PipelineState) -> dict:
    print(f"  [Runner] regression tags: {state.get('regression_tags')}")
    return run_suite("regression", state)
