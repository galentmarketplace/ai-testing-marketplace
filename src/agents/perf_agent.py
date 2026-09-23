"""Performance Agent — enterprise-grade web performance testing.

Two dimensions, because a web app has both:

  * PROTOCOL load (k6) — OPEN-MODEL arrival-rate scenarios (requests arrive independent of system
    state, so the server can't throttle the test and hide its own slowdown), per test TYPE
    (load / stress / soak / spike / breakpoint), with per-endpoint tagged SLOs and a threshold set
    that gates on p95 AND p99 AND a throughput floor AND error rate — not a single averaged number.
  * BROWSER / Core Web Vitals (Playwright) — LCP / CLS / TTFB / FCP against the running web app,
    the user-experience dimension protocol tests can't see. Advisory (an app property, reported not
    "healed"). See [[performance-agent]] for the roadmap (k6 browser hybrid, trend baselines).

The REAL path builds a deterministic k6 script from the repo's analyzed endpoints; the fallback asks
the LLM. No app-specific assumptions.
"""
import json
import os
import re
import subprocess
from pathlib import Path

from .. import runctx
from ..config import GENERATED_DIR, PROJECT_ROOT
from ..llm import call_llm_json
from ..state import PipelineState, TestArtifact

SYSTEM = """You are a senior performance engineer. Given the acceptance criteria and the target app's API
base URL, generate a k6 (JavaScript) performance test for the app's most relevant REST endpoints
(prefer safe, idempotent GET list/summary endpoints).

Rules — make it enterprise-grade, not a toy:
- Use the OPEN model: an arrival-rate executor ('ramping-arrival-rate'), NOT fixed VUs — so the
  system-under-test cannot throttle the test and hide its own slowdown.
- Thresholds must gate on p95 AND p99 AND error rate AND a throughput floor:
  http_req_duration: ['p(95)<800','p(99)<1500'], http_req_failed: ['rate<0.01'], checks: ['rate>0.99'].
- Tag each request with { tags: { endpoint: '<name>' } } and add a per-endpoint threshold
  'http_req_duration{endpoint:<name>}': ['p(95)<1000'] so a slow endpoint fails even if the global passes.
- Parameterize data with SharedArray to avoid cache-hit artifacts. BASE_URL/TOKEN from __ENV. Add think time.

Respond with ONLY a JSON object:
{"files": [{"path": "generated/perf/<name>.k6.js", "covers_ac": ["AC-1"], "tags": ["@perf"], "content": "..."}]}"""

_NODE = "/opt/homebrew/opt/node@20/bin/node"
_TEST_TYPES = ("load", "stress", "soak", "spike", "breakpoint")

# Core Web Vitals thresholds (Google, measured at p75).
_VITALS_LIMITS = {"lcp": ("LCP", 2500, "ms", "loading"), "cls": ("CLS", 0.1, "", "visual stability"),
                  "ttfb": ("TTFB", 800, "ms", "server response"), "fcp": ("FCP", 1800, "ms", "first paint")}


def _target_rps(load: str) -> int:
    """Parse an offered load like '50 rps', '100 req/s', or legacy '10 VUs' → target arrival rate."""
    s = (load or "").lower()
    m = re.search(r"(\d+)\s*(?:rps|req)", s) or re.search(r"(\d+)\s*vus?", s)
    return int(m.group(1)) if m else 50


def _detect_type(inp: dict) -> str:
    t = (inp.get("perf_type") or "").strip().lower()
    if t in _TEST_TYPES:
        return t
    blob = (inp.get("scope_prompt", "") + " " + inp.get("load", "")).lower()
    for tt in _TEST_TYPES:
        if tt in blob:
            return tt
    return "load"


def _scenario(test_type: str, rps: int) -> tuple[str, list[str]]:
    """Return the k6 `scenarios` body + any extra threshold lines for the chosen test TYPE."""
    pav, maxv = max(20, rps), max(50, rps * 5)
    if test_type == "stress":       # push well beyond expected peak to see how it degrades
        stages = f"[{{ target: {rps}, duration: '30s' }}, {{ target: {rps*3}, duration: '2m' }}, {{ target: 0, duration: '20s' }}]"
    elif test_type == "spike":      # sudden surge then recovery
        stages = f"[{{ target: {rps}, duration: '20s' }}, {{ target: {rps*5}, duration: '30s' }}, {{ target: {rps}, duration: '30s' }}, {{ target: 0, duration: '10s' }}]"
    elif test_type == "breakpoint": # ramp until it breaks (abort on error surge)
        stages = f"[{{ target: {rps*10}, duration: '5m' }}]"
    elif test_type == "soak":       # hold steady for a long time (leaks, pool drift)
        body = (f"    {test_type}: {{ executor: 'constant-arrival-rate', rate: {rps}, timeUnit: '1s', "
                f"duration: __ENV.DURATION || '10m', preAllocatedVUs: {pav}, maxVUs: {maxv} }}")
        return body, []
    else:                            # load (default): ramp → hold at expected peak → ramp down
        stages = f"[{{ target: {rps}, duration: __ENV.RAMP || '30s' }}, {{ target: {rps}, duration: __ENV.HOLD || '1m' }}, {{ target: 0, duration: '15s' }}]"
    body = (f"    {test_type}: {{ executor: 'ramping-arrival-rate', startRate: {max(1, rps//5)}, timeUnit: '1s', "
            f"preAllocatedVUs: {pav}, maxVUs: {maxv}, stages: {stages} }}")
    extra = (["    http_req_failed: [{ threshold: 'rate<0.10', abortOnFail: true, delayAbortEval: '30s' }],"]
             if test_type == "breakpoint" else [])
    return body, extra


def _endpoints(analysis: dict) -> list[dict]:
    """Safe, idempotent GET endpoints from the repo's real API surface."""
    eps, seen = [], set()
    for a in analysis.get("api", []):
        for r in a.get("routes", []):
            p = r.get("path", "")
            if r.get("method") != "GET" or "{" in p or ":" in p or p in seen:
                continue
            seen.add(p)
            name = re.sub(r"[^a-zA-Z0-9]+", "_", p.strip("/")) or "root"
            eps.append({"name": name, "path": p})
    # Prefer list/summary endpoints first; cap for a focused test.
    eps.sort(key=lambda e: 0 if (e["path"].endswith("/list") or e["path"].endswith("/summary")) else 1)
    return eps[:6] or [{"name": "root", "path": "/"}]


def _build_real_k6(analysis: dict, base_url: str, inp: dict) -> str:
    test_type = _detect_type(inp)
    rps = _target_rps(inp.get("load", ""))
    eps = _endpoints(analysis)
    scen_body, extra_thresholds = _scenario(test_type, rps)
    ep_js = ",\n".join(f"  {{ name: '{e['name']}', path: '{e['path']}' }}" for e in eps)
    per_ep = [f"    'http_req_duration{{endpoint:{e['name']}}}': ['p(95)<1000']," for e in eps]
    L = [
        "import http from 'k6/http';",
        "import { check, sleep } from 'k6';",
        "import { SharedArray } from 'k6/data';",
        f"// Auto-generated by the Performance agent — OPEN-MODEL {test_type.upper()} test on the repo's real API.",
        f"const BASE = __ENV.BASE_URL || '{base_url}';",
        "const TOKEN = __ENV.TOKEN || '';",
        f"const TARGET_RPS = Number(__ENV.TARGET_RPS || {rps});",
        "// Data parameterization (SharedArray) — spread requests across endpoints to avoid cache-hit artifacts.",
        "const endpoints = new SharedArray('endpoints', () => ([",
        ep_js,
        "]));",
        "export const options = {",
        "  scenarios: {",
        scen_body,
        "  },",
        "  thresholds: {",
        "    http_req_duration: ['p(95)<800', 'p(99)<1500'],   // gate on the tail, not the average",
        f"    http_reqs: ['rate>{max(1, int(rps*0.9))}'],                       // throughput floor: sustain the offered load",
        "    http_req_failed: ['rate<0.01'],",
        "    checks: ['rate>0.99'],",
        *per_ep,
        *extra_thresholds,
        "  },",
        "};",
        "export default function () {",
        "  const ep = endpoints[Math.floor(Math.random() * endpoints.length)];",
        "  const res = http.get(`${BASE}${ep.path}`, { headers: { Authorization: `Bearer ${TOKEN}` }, tags: { endpoint: ep.name } });",
        "  check(res, { [`${ep.name} status ok`]: (r) => r.status >= 200 && r.status < 400 });",
        "  sleep(1);   // think time",
        "}",
        "",
    ]
    return "\n".join(L)


# --------------------------------------------------------------------------- Core Web Vitals (browser)
def _capture_web_vitals(base_url: str) -> dict | None:
    """Launch the running web app with Playwright and collect Core Web Vitals (LCP/CLS/TTFB/FCP).
    Best-effort: returns None if Playwright/app is unavailable. Advisory — never blocks the run."""
    runner = PROJECT_ROOT / "e2e-runner"
    script = runner / "webvitals.cjs"
    if not script.exists() or not (runner / "node_modules" / "playwright").exists():
        return None
    node = _NODE if Path(_NODE).exists() else "node"
    try:
        out = subprocess.run([node, "webvitals.cjs", base_url], cwd=str(runner),
                             capture_output=True, text=True, timeout=90,
                             env={**os.environ, "NODE_PATH": str(runner / "node_modules")})
        line = (out.stdout or "").strip().splitlines()[-1] if out.stdout.strip() else ""
        return json.loads(line) if line else None
    except Exception:
        return None


def _vitals_report(vitals: dict) -> dict:
    checks = []
    for key, (label, limit, unit, what) in _VITALS_LIMITS.items():
        v = vitals.get(key)
        if v is None:
            continue
        ok = v <= limit
        disp = f"{v:.2f}" if key == "cls" else f"{v:.0f}{unit}"
        checks.append({"label": f"{label} {disp} ({what})", "threshold": f"< {limit}{unit}", "ok": ok})
    passed = sum(1 for c in checks if c["ok"])
    return {"vitals": vitals, "checks": checks, "passed": passed, "total": len(checks),
            "note": "INP needs field/interaction data — captured LCP/CLS/TTFB/FCP (lab)."}


def generate_perf_scripts(state: PipelineState) -> dict:
    inp = state.get("story", {}).get("inputs", {}) or {}
    analysis = state.get("repo_analysis")
    attempts = dict(state.get("attempts", {}))
    attempts["generate_perf_scripts"] = attempts.get("generate_perf_scripts", 0) + 1
    artifacts = list(state.get("test_artifacts", []))
    out_state = {"test_artifacts": artifacts, "attempts": attempts}

    # BROWSER dimension: Core Web Vitals against the running web app (advisory, real path only).
    base_web = inp.get("base_url")
    if base_web and not runctx.is_mock():
        vitals = _capture_web_vitals(base_web.rstrip("/"))
        if vitals:
            rep = _vitals_report(vitals)
            vout = GENERATED_DIR / "perf" / "web-vitals.json"
            vout.parent.mkdir(parents=True, exist_ok=True)
            vout.write_text(json.dumps(rep, indent=2))
            artifacts.append(TestArtifact(type="web-vitals", path=str(vout), tags=["@perf", "@webvitals"]).model_dump())
            out_state["perf_vitals"] = rep
            print(f"  [Perf Agent] Core Web Vitals: {rep['passed']}/{rep['total']} within budget")

    # PROTOCOL dimension: k6. REAL path builds against the repo's analyzed endpoints.
    if analysis and analysis.get("ok") and analysis.get("api"):
        base = inp.get("base_url") or "http://localhost:8888"
        test_type = _detect_type(inp)
        content = _build_real_k6(analysis, base, inp)
        out = GENERATED_DIR / "perf" / f"api-{test_type}.k6.js"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(content)
        artifacts.append(TestArtifact(type="k6", path=str(out), tags=["@perf", "@real", f"@{test_type}"]).model_dump())
        print(f"  [Perf Agent] attempt {attempts['generate_perf_scripts']}: open-model {test_type} k6 for {base} "
              f"({content.count('endpoint:')} endpoint SLOs)")
        return out_state

    # Fallback: LLM/mock generation when no repo was analyzed.
    ac = state.get("acceptance_criteria", "(none — perf run against known endpoints)")
    ctx = f"\nAPI base URL: {inp['base_url']}" if inp.get("base_url") else ""
    raw = call_llm_json("perf_agent", SYSTEM, f"Acceptance criteria:\n{ac}{ctx}")
    for f in raw["files"]:
        fout = GENERATED_DIR / Path(f["path"]).relative_to("generated")
        fout.parent.mkdir(parents=True, exist_ok=True)
        fout.write_text(f["content"])
        artifacts.append(TestArtifact(type="k6", path=str(fout),
                                      covers_ac=f.get("covers_ac", []), tags=f.get("tags", [])).model_dump())
    print(f"  [Perf Agent] attempt {attempts['generate_perf_scripts']}: wrote {len(raw['files'])} k6 script(s)")
    return out_state
