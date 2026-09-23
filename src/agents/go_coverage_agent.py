"""Go Coverage agent — measures the REAL code coverage of a Go repository and gates on it.

Deterministic (no LLM): runs the repo's own tests with `go test ./... -coverprofile -covermode=atomic`,
parses statement coverage + per-function detail, and emits the STANDARD interchange formats so IDEs
and CI consume it directly: LCOV (VS Code Test Coverage API / Codecov / Sonar) + Cobertura XML
(Jenkins / GitLab / Azure). See docs/STANDARDS.md §3.

Gate is observe-first: ADVISORY by default (report + "would block below X%"); set
inputs.coverage_blocking=yes to fail the COVERAGE gate below the threshold. Never self-heal-loops
(there is no Go test generator yet — that is the EarlyAI-style phase 2).
"""
import json

from .. import runctx
from ..config import GATE_POLICY, GENERATED_DIR
from ..integration import go_coverage as gocov
from ..integration import reports
from ..integration.repo_analyzer import _ensure_local
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


def _min_pct(inp: dict) -> float:
    v = str(inp.get("min_coverage") or "").strip().rstrip("%")
    try:
        f = float(v)
        return f * 100 if f <= 1 else f
    except ValueError:
        return GATE_POLICY["COVERAGE"]["min_coverage"] * 100


_MOCK_LINES = {"internal/handler/handler.go": {**{i: 3 for i in range(10, 40)}, **{i: 0 for i in range(40, 52)}},
               "internal/service/order.go": {**{i: 2 for i in range(8, 60)}, **{i: 0 for i in range(60, 66)}},
               "pkg/util/strings.go": {i: 1 for i in range(5, 25)}}
_MOCK = {"ok": True, "tool": "go test -coverprofile (mock)", "module_dir": "repo", "tests_passed": True,
         "total_pct": 74.2, "statements": 310, "statements_covered": 230,
         "files": [{"file": "internal/handler/handler.go", "lines": 42, "covered": 30, "pct": 71.4},
                   {"file": "internal/service/order.go", "lines": 58, "covered": 52, "pct": 89.7},
                   {"file": "pkg/util/strings.go", "lines": 20, "covered": 20, "pct": 100.0}],
         "lines": _MOCK_LINES,
         "funcs": [], "uncovered_funcs": [{"file": "internal/handler/handler.go", "line": 40, "func": "handleRefund", "pct": 0.0},
                                          {"file": "internal/service/order.go", "line": 60, "func": "cancelOrder", "pct": 0.0}],
         "test_output_tail": "ok  \tinternal/handler\t0.412s\tcoverage: 71.4% of statements (mock)"}


def go_coverage(state: PipelineState) -> dict:
    inp = state.get("story", {}).get("inputs", {}) or {}
    repo = inp.get("source_repo") or inp.get("repo") or "repo"
    min_pct = _min_pct(inp)

    if runctx.is_mock():
        rep = dict(_MOCK)
    else:
        path = _repo_path(state)
        rep = gocov.run_go_coverage(path) if path else {"ok": False, "error": "no repo path (configure a source repository)"}
        if rep.get("ok"):
            print(f"  [Go Coverage] measuring {path} …")

    out_dir = GENERATED_DIR / "coverage"
    out_dir.mkdir(parents=True, exist_ok=True)
    arts = list(state.get("coverage_artifacts", []))
    (out_dir / "coverage-report.md").write_text(gocov.report_markdown(rep, repo, min_pct))
    arts.append({"type": "coverage-report", "path": str(out_dir / "coverage-report.md"), "tags": ["@coverage"]})
    summary = {k: v for k, v in rep.items() if k != "lines"}
    summary["min_pct"] = min_pct
    if rep.get("ok"):
        cov = {"lines": rep["lines"], "total_pct": rep["total_pct"]}
        (out_dir / "coverage.json").write_text(json.dumps(summary, indent=2))
        (out_dir / "coverage.lcov").write_text(reports.lcov(cov))          # VS Code / Codecov / Sonar
        (out_dir / "cobertura.xml").write_text(reports.cobertura_xml(cov))  # Jenkins / GitLab / Azure
        arts += [{"type": "coverage-json", "path": str(out_dir / "coverage.json"), "tags": ["@coverage"]},
                 {"type": "coverage-lcov", "path": str(out_dir / "coverage.lcov"), "tags": ["@coverage", "@lcov"]},
                 {"type": "coverage-cobertura", "path": str(out_dir / "cobertura.xml"), "tags": ["@coverage", "@cobertura"]}]
        print(f"  [Go Coverage] {rep['total_pct']:.1f}% of statements ({rep['statements_covered']}/{rep['statements']}), "
              f"{len(rep['uncovered_funcs'])} uncovered function(s) · threshold {min_pct:.0f}%")
    else:
        print(f"  [Go Coverage] not measured — {rep.get('error')}")
    return {"coverage_artifacts": arts, "coverage_report": summary}


def coverage_gate(state: PipelineState) -> dict:
    rep = state.get("coverage_report", {}) or {}
    inp = state.get("story", {}).get("inputs", {}) or {}
    blocking = str(inp.get("coverage_blocking", "")).strip().lower() in ("1", "true", "yes", "y")
    min_pct = rep.get("min_pct", GATE_POLICY["COVERAGE"]["min_coverage"] * 100)

    if not rep.get("ok"):
        if rep.get("not_applicable"):
            verdict, reason = "pass", "Not a Go repository — coverage step skipped."
            checks = [{"label": "no go.mod detected", "threshold": "Go repo", "ok": True, "advisory": True}]
        else:
            verdict = "fail" if blocking else "pass"
            reason = f"Coverage could not be measured: {rep.get('error', 'unknown error')}"
            checks = [{"label": reason, "threshold": "go test -coverprofile must run", "ok": False, "advisory": not blocking}]
    else:
        tot = rep.get("total_pct_after", rep["total_pct"])   # verified post-generation number when available
        below = tot < min_pct
        prop = rep.get("proposed_tests") or {}
        verdict = "fail" if (below and blocking) else "pass"
        reason = (f"Coverage {tot:.1f}% vs {min_pct:.0f}% threshold — "
                  + ("BELOW" + (" (blocking)" if blocking else " (advisory — would block)") if below else "meets policy")
                  + f"; {len(rep.get('uncovered_funcs', []))} uncovered function(s).")
        checks = [
            {"label": f"statement coverage {tot:.1f}%", "threshold": f"≥ {min_pct:.0f}%", "ok": not below, "advisory": not blocking},
            {"label": "repo tests " + ("passed" if rep.get("tests_passed") else "FAILED"), "threshold": "green", "ok": bool(rep.get("tests_passed")), "advisory": True},
            {"label": f"{len(rep.get('uncovered_funcs', []))} uncovered function(s)", "threshold": "review", "ok": True, "advisory": True},
            {"label": "LCOV + Cobertura exported", "threshold": "standard formats", "ok": True},
        ]
        if prop:
            checks.append({"label": f"{prop.get('proposed', 0)} generated test file(s) for {len(prop.get('covers', []))} uncovered function(s) — "
                                    + ("verified" if prop.get("verified") else "unverified") + (f", coverage → {rep['total_pct_after']}%" if rep.get("total_pct_after") is not None else ""),
                           "threshold": "execution-verified", "ok": bool(prop.get("verified")), "advisory": True})
    decision = GateDecision(gate="COVERAGE", verdict=verdict, reason=reason, checks=checks, route_to=None)
    print(f"  [Gate COVERAGE] {verdict.upper()} — {reason}")
    return {"gate_decisions": state.get("gate_decisions", []) + [decision.model_dump()]}
