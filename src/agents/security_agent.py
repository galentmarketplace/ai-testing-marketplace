"""Security Agent — runs a REAL whole-repo SAST scan (Semgrep) and reports findings.

Security is an AUDIT, not a pass/fail feature gate: findings are surfaced in a report,
not "self-healed" away. So this capability:
  * generate_security_scan — picks the scan plan (Semgrep ruleset) for the repo's stack;
  * run_security_scan      — runs Semgrep over the ENTIRE cloned repo, writes a Markdown
                             + JSON report, and records coverage (files/modules scanned);
  * security_gate          — ADVISORY: passes when the scan RAN successfully (report
                             delivered). It never loops — it reports risk, it doesn't block.
"""
import json
import os

from ..config import GENERATED_DIR
from ..integration import security_scan
from ..integration.repo_analyzer import _ensure_local
from ..mocks import MOCK_RESPONSES
from ..state import GateDecision, PipelineState

MOCK_RESPONSES.setdefault("security_agent", '{"plan": "p/default"}')


def _repo_path(state: PipelineState) -> str | None:
    """The local checkout to scan — from repo analysis, or by resolving the configured repo."""
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


def generate_security_scan(state: PipelineState) -> dict:
    """Decide the scan PLAN — which methodologies apply to this repo/stack. Deterministic (no LLM)."""
    attempts = dict(state.get("attempts", {}))
    attempts["generate_security_scan"] = attempts.get("generate_security_scan", 0) + 1
    stack = (state.get("repo_analysis") or {}).get("stack", "web app")
    plan = {"stack": stack,
            "methodologies": [
                {"id": "sast", "label": "Static analysis (SAST)", "tool": "Semgrep"},
                {"id": "sca", "label": "Dependencies & CVEs (SCA)", "tool": "OSV-Scanner / npm audit / pip-audit"},
                {"id": "secrets", "label": "Secret scanning", "tool": "Gitleaks / built-in"},
                {"id": "iac", "label": "IaC / config misconfig", "tool": "Checkov"},
                {"id": "container", "label": "Container / image scan", "tool": "Trivy"},
                {"id": "sbom", "label": "SBOM & license inventory", "tool": "Syft (CycloneDX)"},
                {"id": "dast", "label": "Dynamic (DAST) / API security", "tool": "OWASP ZAP (roadmap)"},
            ],
            "standards": ["OWASP Top 10", "CWE", "CVSS", "SARIF"]}
    out = GENERATED_DIR / "security" / "scan-plan.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(plan, indent=2))
    arts = list(state.get("security_artifacts", []))
    arts.append({"type": "sast-plan", "path": str(out), "tags": ["@security"]})
    print(f"  [Security Agent] scan plan: {len(plan['methodologies'])} methodolog(ies) for {stack}")
    return {"security_artifacts": arts, "attempts": attempts}


_MOCK_REPORT = {
    "ok": True, "config": "multi-scanner", "files_scanned": 214,
    "modules_scanned": ["backend", "frontend"], "total": 6,
    "by_severity": {"critical": 1, "high": 1, "medium": 2, "low": 2, "info": 0},
    "by_module": {"backend": 4, "frontend": 2},
    "by_methodology": {"sast": 2, "sca": 2, "secrets": 1, "iac": 1},
    "methodologies": [
        {"methodology": "sast", "label": "Static analysis (SAST)", "tool": "Semgrep p/default", "ran": True, "count": 2, "note": "", "install": None},
        {"methodology": "sca", "label": "Dependencies & CVEs (SCA)", "tool": "npm audit", "ran": True, "count": 2, "note": "", "install": None},
        {"methodology": "secrets", "label": "Secret scanning", "tool": "built-in", "ran": True, "count": 1, "note": "", "install": "brew install gitleaks"},
        {"methodology": "iac", "label": "IaC / config misconfig", "tool": "Checkov", "ran": True, "count": 1, "note": "", "install": None},
        {"methodology": "container", "label": "Container / image scan", "tool": "Trivy", "ran": False, "count": 0, "note": "no Dockerfile detected", "install": None},
        {"methodology": "dast", "label": "Dynamic (DAST) / API security", "tool": "OWASP ZAP", "ran": False, "count": 0, "note": "planned — needs the running app", "install": None},
    ],
    "methodologies_run": ["Static analysis (SAST)", "Dependencies & CVEs (SCA)", "Secret scanning", "IaC / config misconfig"],
    "to_enable": [{"label": "Container / image scan", "tool": "Trivy", "install": "brew install trivy", "note": ""}],
    "owasp_coverage": ["A01 Broken Access Control", "A02 Cryptographic Failures", "A03 Injection",
                       "A05 Security Misconfiguration", "A06 Vulnerable & Outdated Components", "A07 Identification & Auth Failures"],
    "top_rules": [["detected-generic-secret", 1], ["CVE-2023-1234", 1], ["missing-csrf", 1]],
    "findings": [
        {"methodology": "secrets", "tool": "built-in", "rule": "aws-access-key-id", "severity": "critical", "cwe": "CWE-798", "owasp": "A07 Identification & Auth Failures", "cvss": None, "package": None, "file": "backend/.env", "line": 2, "message": "Possible hardcoded secret (aws-access-key-id) (mock)"},
        {"methodology": "sca", "tool": "npm audit", "rule": "lodash", "severity": "high", "cwe": "CWE-1321", "owasp": "A06 Vulnerable & Outdated Components", "cvss": 7.4, "package": "lodash", "file": "frontend/package.json", "line": None, "message": "Prototype pollution in lodash (mock)"},
    ],
    "errors": [],
}


def run_security_scan(state: PipelineState) -> dict:
    """Run Semgrep over the whole repo and write the report (simulated under MOCK_LLM)."""
    repo = (state.get("story", {}).get("inputs", {}) or {}).get("source_repo") \
        or (state.get("story", {}).get("inputs", {}) or {}).get("repo") or "repo"
    if os.environ.get("MOCK_LLM") == "1":
        report = dict(_MOCK_REPORT)
    else:
        path = _repo_path(state)
        if not path:
            report = {"ok": False, "error": "no repo path to scan (configure a source repository)"}
        else:
            print(f"  [Security Scan] scanning {path} with Semgrep …")
            report = security_scan.scan_repo(path)

    arts = list(state.get("security_artifacts", []))
    md = security_scan.report_markdown(report, repo)
    md_out = GENERATED_DIR / "security" / "security-report.md"
    md_out.parent.mkdir(parents=True, exist_ok=True)
    md_out.write_text(md)
    arts.append({"type": "sast-report", "path": str(md_out), "tags": ["@security"]})
    if report.get("ok"):
        js_out = GENERATED_DIR / "security" / "security-report.json"
        js_out.write_text(json.dumps(report, indent=2))
        arts.append({"type": "sast-json", "path": str(js_out), "tags": ["@security"]})
        # SARIF — the enterprise interchange format (GitHub Security tab, CWE/CVSS).
        sarif_out = GENERATED_DIR / "security" / "security.sarif"
        sarif_out.write_text(json.dumps(security_scan.report_sarif(report), indent=2))
        arts.append({"type": "sast-sarif", "path": str(sarif_out), "tags": ["@security"]})
        s = report["by_severity"]
        print(f"  [Security Scan] {', '.join(report.get('methodologies_run', [])) or 'no scanners'} → "
              f"{report['total']} finding(s) (C{s.get('critical', 0)} H{s['high']} M{s['medium']} L{s['low']})")

    # Record as a run so it appears in the results; findings do NOT fail the run.
    rr = list(state.get("run_results", []))
    rr.append({"run_id": "semgrep", "suite": "security",
               "passed": report["files_scanned"] if report.get("ok") else 0,
               "failed": 0, "failures": []})
    return {"security_artifacts": arts, "run_results": rr, "security_report": report}


def security_gate(state: PipelineState) -> dict:
    """ADVISORY gate — passes when the scan ran and a report exists. Surfaces risk counts,
    never loops (security findings are triaged by humans, not auto-healed)."""
    rep = state.get("security_report", {}) or {}
    if not rep.get("ok"):
        verdict, reason = "fail", f"Security scan could not run: {rep.get('error', 'unknown error')}"
        checks = [{"label": reason, "threshold": "scan must run", "ok": False}]
    else:
        s = rep["by_severity"]
        crit, high = s.get("critical", 0), s.get("high", 0)
        ran = rep.get("methodologies_run", [])
        # Advisory / observe-first: a completed scan PASSES; findings are triaged by humans, not auto-healed.
        # Severity-aware "would block" signals are surfaced so the enterprise gate story is explicit.
        verdict = "pass"
        reason = (f"Scan complete — {len(ran)} methodolog(ies) [{', '.join(ran) or '—'}]; "
                  f"{rep['total']} finding(s): {crit} critical / {high} high / {s['medium']} med / {s['low']} low.")
        checks = [
            {"label": f"{len(ran)} methodolog(ies) run", "threshold": "coverage", "ok": len(ran) > 0},
            {"label": f"{crit} critical finding(s)", "threshold": "would block at release", "ok": crit == 0},
            {"label": f"{high} high-severity finding(s)", "threshold": "would block at PR", "ok": high == 0},
            {"label": f"OWASP coverage: {len(rep.get('owasp_coverage', []))} categor(ies)", "threshold": "Top 10", "ok": True},
        ]
        if rep.get("to_enable"):
            checks.append({"label": f"{len(rep['to_enable'])} more methodolog(ies) available to enable",
                           "threshold": "install to activate", "ok": True})
    decision = GateDecision(gate="SEC", verdict=verdict, reason=reason, checks=checks, route_to=None)
    print(f"  [Gate SEC] {verdict.upper()} — {reason}")
    return {"gate_decisions": state.get("gate_decisions", []) + [decision.model_dump()]}
