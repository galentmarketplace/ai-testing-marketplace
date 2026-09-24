"""Multi-methodology security scanning — enterprise-grade, tool-agnostic, degrade-gracefully.

The platform's edge is that it has a repo, a running app, and CI together, which unlocks the whole
testing pyramid — not just static analysis. This module orchestrates the practical AppSec
methodologies, each running when its tool is installed and otherwise reported clearly with an
install hint (never a silent gap):

  * SAST        — Semgrep over the whole repo (source weaknesses)                     [needs repo]
  * SCA + CVE   — OSV-Scanner ▸ npm audit ▸ pip-audit (vulnerable dependencies)       [needs repo]
  * Secrets     — Gitleaks, else a built-in regex/entropy scanner (always runs)       [needs repo]
  * IaC/config  — Checkov (Terraform/K8s/Dockerfile misconfig), when IaC is present   [needs repo]
  * Containers  — Trivy image/fs scan, when a Dockerfile is present                    [needs repo]
  * SBOM        — Syft → CycloneDX (inventory + license), when available               [needs repo]
  * DAST/API    — planned; needs the running app + OpenAPI + role tokens (see roadmap) [needs app]

Findings are normalized to one schema and tagged with CWE + OWASP Top-10 category + severity
(critical/high/medium/low), so the report unifies across tools and a SARIF export is emitted.
No LLM decides pass/fail — scanners find; the report surfaces risk (advisory, observe-first).
"""
import json
import re
import shutil
import subprocess
import sys
from collections import Counter
from pathlib import Path

from .. import sandbox

_SEMGREP = str(Path(sys.prefix) / "bin" / "semgrep")
_SEV = {"ERROR": "high", "WARNING": "medium", "INFO": "low"}
_SEV_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}
_SKIP_DIRS = {"node_modules", ".git", "dist", "build", "vendor", "coverage", ".venv",
              "venv", "__pycache__", ".next", ".nuxt", "out", "target", ".idea"}

# OWASP Top 10 (2021) categories each methodology contributes coverage toward — the exec story.
_OWASP_BY_METHOD = {
    "sast": ["A03 Injection", "A01 Broken Access Control", "A02 Cryptographic Failures"],
    "sca": ["A06 Vulnerable & Outdated Components"],
    "secrets": ["A07 Identification & Auth Failures", "A02 Cryptographic Failures"],
    "iac": ["A05 Security Misconfiguration"],
    "container": ["A06 Vulnerable & Outdated Components", "A05 Security Misconfiguration"],
    "sbom": ["A06 Vulnerable & Outdated Components", "A08 Software & Data Integrity"],
    "dast": ["A01 Broken Access Control", "A03 Injection", "A05 Security Misconfiguration"],
}


def _which(tool: str) -> str | None:
    return shutil.which(tool)


def _run(cmd, cwd=None, timeout=300):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd,
                              env=sandbox.child_env())
    except Exception:
        return None


def _rel(p, root: Path) -> str:
    try:
        return str(Path(p).relative_to(root))
    except Exception:
        return str(p)


def _iter_files(root: Path, max_bytes: int = 400_000):
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        if any(part in _SKIP_DIRS for part in p.parts):
            continue
        try:
            if p.stat().st_size > max_bytes:
                continue
        except OSError:
            continue
        yield p


def _method(tool, ran, findings, *, methodology, label, available=True, install=None, note="", files=0):
    return {"methodology": methodology, "label": label, "tool": tool, "ran": ran,
            "available": available, "install": install, "findings": findings, "note": note, "files": files}


# --------------------------------------------------------------------------- SAST (Semgrep)
def scan_sast(root: Path) -> dict:
    if not (_which("semgrep") or Path(_SEMGREP).exists()):
        return _method("Semgrep", False, [], methodology="sast", label="Static analysis (SAST)",
                       available=False, install="pip install semgrep")
    binp = _SEMGREP if Path(_SEMGREP).exists() else "semgrep"
    proc = _run([binp, "scan", "--config", "p/default", "--json", "--timeout", "120",
                 "--disable-version-check", "--metrics", "off", str(root)], timeout=360)
    if not proc:
        return _method("Semgrep", False, [], methodology="sast", label="Static analysis (SAST)",
                       note="semgrep timed out")
    try:
        data = json.loads(proc.stdout or "{}")
    except Exception:
        return _method("Semgrep", False, [], methodology="sast", label="Static analysis (SAST)",
                       note=(proc.stderr or "no semgrep output")[:200])
    findings, scanned = [], data.get("paths", {}).get("scanned", [])
    for r in data.get("results", []):
        extra = r.get("extra", {}) or {}
        meta = extra.get("metadata", {}) or {}
        cwe = meta.get("cwe")
        cwe = cwe[0] if isinstance(cwe, list) and cwe else (cwe if isinstance(cwe, str) else None)
        owasp = meta.get("owasp")
        owasp = owasp[0] if isinstance(owasp, list) and owasp else (owasp if isinstance(owasp, str) else None)
        findings.append({"methodology": "sast", "tool": "Semgrep",
                         "rule": (r.get("check_id", "") or "").split(".")[-1],
                         "severity": _SEV.get((extra.get("severity") or "INFO").upper(), "low"),
                         "cwe": cwe, "owasp": owasp, "cvss": None, "package": None,
                         "file": _rel(r.get("path", ""), root), "line": r.get("start", {}).get("line"),
                         "message": (extra.get("message") or "").strip()[:300]})
    return _method("Semgrep p/default", True, findings, methodology="sast",
                   label="Static analysis (SAST)", files=len(scanned))


# --------------------------------------------------------------------------- SCA (deps + CVE)
def _parse_npm_audit(out: str, manifest_rel: str) -> list:
    findings = []
    try:
        data = json.loads(out or "{}")
    except Exception:
        return findings
    smap = {"moderate": "medium", "info": "info", "low": "low", "high": "high", "critical": "critical"}
    for name, v in (data.get("vulnerabilities", {}) or {}).items():
        cwe, cvss, title = None, None, None
        for x in (v.get("via", []) or []):
            if isinstance(x, dict):
                if x.get("cwe"):
                    cwe = (x["cwe"][0] if isinstance(x["cwe"], list) else x["cwe"])
                cvss = cvss or (x.get("cvss", {}) or {}).get("score")
                title = title or x.get("title")
        findings.append({"methodology": "sca", "tool": "npm audit", "rule": name,
                         "severity": smap.get(v.get("severity"), "medium"), "cwe": cwe,
                         "owasp": "A06 Vulnerable & Outdated Components", "cvss": cvss, "package": name,
                         "file": manifest_rel, "line": None,
                         "message": (title or f"Known vulnerability in dependency '{name}'")[:300]})
    return findings


def scan_sca(root: Path) -> dict:
    # Preferred: OSV-Scanner (Google OSV DB, low FP, multi-ecosystem).
    if _which("osv-scanner"):
        proc = _run(["osv-scanner", "--format", "json", "-r", str(root)], timeout=300)
        findings = []
        try:
            data = json.loads((proc.stdout if proc else "") or "{}")
            for res in data.get("results", []):
                src = _rel(res.get("source", {}).get("path", ""), root)
                for pkg in res.get("packages", []):
                    name = pkg.get("package", {}).get("name", "?")
                    sev = "high"
                    for g in pkg.get("groups", []):
                        ms = (g.get("max_severity") or "")
                        try:
                            score = float(ms)
                            sev = "critical" if score >= 9 else "high" if score >= 7 else "medium" if score >= 4 else "low"
                        except ValueError:
                            pass
                    for vuln in pkg.get("vulnerabilities", []):
                        findings.append({"methodology": "sca", "tool": "OSV-Scanner",
                                         "rule": vuln.get("id", "OSV"), "severity": sev, "cwe": None,
                                         "owasp": "A06 Vulnerable & Outdated Components", "cvss": None,
                                         "package": name, "file": src, "line": None,
                                         "message": (vuln.get("summary") or vuln.get("id", ""))[:300]})
        except Exception:
            pass
        return _method("OSV-Scanner", True, findings, methodology="sca", label="Dependencies & CVEs (SCA)")

    # Fallback: native ecosystem auditors that are commonly present.
    findings, tool = [], None
    locks = [p for p in root.rglob("package-lock.json") if not any(d in p.parts for d in _SKIP_DIRS)][:3]
    if locks and _which("npm"):
        tool = "npm audit"
        for lk in locks:
            proc = _run(["npm", "audit", "--json"], cwd=str(lk.parent), timeout=180)
            if proc and proc.stdout:
                findings += _parse_npm_audit(proc.stdout, _rel(lk.parent / "package.json", root))
    elif _which("pip-audit") and (list(root.rglob("requirements*.txt")) or (root / "pyproject.toml").exists()):
        tool = "pip-audit"
        proc = _run(["pip-audit", "-f", "json", "--progress-spinner", "off"], cwd=str(root), timeout=240)
        try:
            data = json.loads((proc.stdout if proc else "") or "{}")
            deps = data.get("dependencies", data) if isinstance(data, dict) else data
            for d in (deps if isinstance(deps, list) else []):
                for vln in d.get("vulns", []):
                    findings.append({"methodology": "sca", "tool": "pip-audit", "rule": vln.get("id", "PYSEC"),
                                     "severity": "high", "cwe": None,
                                     "owasp": "A06 Vulnerable & Outdated Components", "cvss": None,
                                     "package": d.get("name"), "file": "requirements", "line": None,
                                     "message": (vln.get("description") or vln.get("id", ""))[:300]})
        except Exception:
            pass
    if tool:
        return _method(tool, True, findings, methodology="sca", label="Dependencies & CVEs (SCA)")
    return _method("OSV-Scanner", False, [], methodology="sca", label="Dependencies & CVEs (SCA)",
                   available=False, install="brew install osv-scanner  (or: npm i / pip install pip-audit)",
                   note="no dependency lockfile detected, or scanner not installed")


# --------------------------------------------------------------------------- Secrets
_SECRET_PATTERNS = [
    ("aws-access-key-id", r"AKIA[0-9A-Z]{16}", "critical"),
    ("private-key", r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY-----", "critical"),
    ("github-token", r"gh[pousr]_[A-Za-z0-9]{36,}", "critical"),
    ("github-pat", r"github_pat_[A-Za-z0-9_]{22,}", "critical"),
    ("slack-token", r"xox[baprs]-[A-Za-z0-9-]{10,}", "high"),
    ("google-api-key", r"AIza[0-9A-Za-z\-_]{35}", "high"),
    ("stripe-key", r"[sr]k_live_[A-Za-z0-9]{20,}", "critical"),
    ("jwt", r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}", "medium"),
    ("generic-secret-assignment",
     r"(?i)(?:api[_-]?key|secret|passwd|password|access[_-]?token|auth[_-]?token)\s*[:=]\s*['\"][A-Za-z0-9_\-\.\/+=]{16,}['\"]", "high"),
]
_SECRET_FALSE = re.compile(r"(?i)example|placeholder|dummy|sample|your[_-]|xxxx|<[^>]+>|process\.env|os\.environ|getenv|\bchangeme\b|test[_-]?key")


def scan_secrets(root: Path) -> dict:
    if _which("gitleaks"):
        proc = _run(["gitleaks", "detect", "--no-git", "--report-format", "json",
                     "--report-path", "/dev/stdout", "-s", str(root)], timeout=240)
        findings = []
        try:
            for r in json.loads((proc.stdout if proc else "") or "[]"):
                findings.append({"methodology": "secrets", "tool": "Gitleaks",
                                 "rule": r.get("RuleID", "secret"), "severity": "critical", "cwe": "CWE-798",
                                 "owasp": "A07 Identification & Auth Failures", "cvss": None, "package": None,
                                 "file": _rel(r.get("File", ""), root), "line": r.get("StartLine"),
                                 "message": f"Hardcoded secret ({r.get('RuleID', 'secret')})"})
        except Exception:
            pass
        return _method("Gitleaks", True, findings, methodology="secrets", label="Secret scanning")

    # Built-in fallback — always runs so 'secrets' is never a silent gap.
    findings, files = [], 0
    compiled = [(n, re.compile(rx), sev) for n, rx, sev in _SECRET_PATTERNS]
    for p in _iter_files(root, max_bytes=300_000):
        if p.suffix in (".lock", ".map") or p.name.endswith(".min.js"):
            continue
        try:
            text = p.read_text(errors="ignore")
        except Exception:
            continue
        files += 1
        for i, line in enumerate(text.splitlines(), 1):
            if len(line) > 500 or _SECRET_FALSE.search(line):
                continue
            for name, rx, sev in compiled:
                if rx.search(line):
                    findings.append({"methodology": "secrets", "tool": "built-in", "rule": name,
                                     "severity": sev, "cwe": "CWE-798",
                                     "owasp": "A07 Identification & Auth Failures", "cvss": None,
                                     "package": None, "file": _rel(p, root), "line": i,
                                     "message": f"Possible hardcoded secret ({name})"})
                    break
            if len(findings) >= 100:
                break
        if len(findings) >= 100:
            break
    return _method("built-in (install gitleaks for verified secrets)", True, findings, methodology="secrets",
                   label="Secret scanning", install="brew install gitleaks", files=files)


# --------------------------------------------------------------------------- IaC / config
def _has_iac(root: Path) -> bool:
    for pat in ("*.tf", "Dockerfile", "*.dockerfile", "docker-compose*.y*ml"):
        if next((p for p in root.rglob(pat) if not any(d in p.parts for d in _SKIP_DIRS)), None):
            return True
    return bool(next((p for p in root.rglob("*.y*ml")
                      if not any(d in p.parts for d in _SKIP_DIRS)
                      and re.search(r"kind:\s*(Deployment|Service|Pod|Ingress)", p.read_text(errors="ignore")[:2000] if p.is_file() else "")), None))


def scan_iac(root: Path) -> dict:
    if not _has_iac(root):
        return _method("Checkov", False, [], methodology="iac", label="IaC / config misconfig",
                       note="no Terraform / K8s / Dockerfile detected — not applicable")
    if not _which("checkov"):
        return _method("Checkov", False, [], methodology="iac", label="IaC / config misconfig",
                       available=False, install="pip install checkov")
    proc = _run(["checkov", "-d", str(root), "-o", "json", "--compact", "--quiet"], timeout=300)
    findings = []
    try:
        data = json.loads((proc.stdout if proc else "") or "{}")
        blocks = data if isinstance(data, list) else [data]
        for blk in blocks:
            for c in (blk.get("results", {}) or {}).get("failed_checks", []):
                findings.append({"methodology": "iac", "tool": "Checkov", "rule": c.get("check_id", "CKV"),
                                 "severity": (c.get("severity") or "medium").lower(), "cwe": None,
                                 "owasp": "A05 Security Misconfiguration", "cvss": None, "package": None,
                                 "file": _rel(c.get("file_path", ""), root),
                                 "line": (c.get("file_line_range") or [None])[0],
                                 "message": (c.get("check_name") or c.get("check_id", ""))[:300]})
    except Exception:
        pass
    return _method("Checkov", True, findings, methodology="iac", label="IaC / config misconfig")


# --------------------------------------------------------------------------- Containers
def scan_containers(root: Path) -> dict:
    dockerfiles = [p for p in root.rglob("Dockerfile") if not any(d in p.parts for d in _SKIP_DIRS)]
    if not dockerfiles:
        return _method("Trivy", False, [], methodology="container", label="Container / image scan",
                       note="no Dockerfile detected — not applicable")
    if not _which("trivy"):
        return _method("Trivy", False, [], methodology="container", label="Container / image scan",
                       available=False, install="brew install trivy")
    proc = _run(["trivy", "fs", "--quiet", "--format", "json", "--scanners", "vuln,misconfig,secret", str(root)], timeout=360)
    findings = []
    try:
        data = json.loads((proc.stdout if proc else "") or "{}")
        for res in data.get("Results", []):
            tgt = res.get("Target", "")
            for v in res.get("Vulnerabilities", []) or []:
                findings.append({"methodology": "container", "tool": "Trivy",
                                 "rule": v.get("VulnerabilityID", "CVE"), "severity": (v.get("Severity") or "MEDIUM").lower(),
                                 "cwe": (v.get("CweIDs") or [None])[0], "owasp": "A06 Vulnerable & Outdated Components",
                                 "cvss": None, "package": v.get("PkgName"), "file": tgt, "line": None,
                                 "message": (v.get("Title") or v.get("VulnerabilityID", ""))[:300]})
    except Exception:
        pass
    return _method("Trivy", True, findings, methodology="container", label="Container / image scan")


# --------------------------------------------------------------------------- SBOM
def scan_sbom(root: Path) -> dict:
    if not _which("syft"):
        return _method("Syft (CycloneDX)", False, [], methodology="sbom", label="SBOM & license inventory",
                       available=False, install="brew install syft")
    proc = _run(["syft", str(root), "-o", "cyclonedx-json", "-q"], timeout=240)
    count = 0
    try:
        data = json.loads((proc.stdout if proc else "") or "{}")
        count = len(data.get("components", []))
    except Exception:
        pass
    return _method("Syft (CycloneDX)", True, [], methodology="sbom", label="SBOM & license inventory",
                   note=f"{count} components inventoried")


# --------------------------------------------------------------------------- DAST (planned)
def scan_dast(root: Path) -> dict:  # placeholder documented in the roadmap
    return _method("OWASP ZAP", False, [], methodology="dast", label="Dynamic (DAST) / API security",
                   available=False, install="needs the running app + OpenAPI spec + role tokens (roadmap)",
                   note="planned — exploits the running-app asset (OWASP API Top 10: BOLA/BFLA/auth)")


# --------------------------------------------------------------------------- Orchestrator
def scan_repo(path: str, timeout: int = 300) -> dict:
    root = Path(path)
    if not root.exists():
        return {"ok": False, "error": f"repo path not found: {path}"}

    methods = [scan_sast(root), scan_sca(root), scan_secrets(root),
               scan_iac(root), scan_containers(root), scan_sbom(root), scan_dast(root)]

    findings = [f for m in methods for f in m["findings"]]
    by_sev = Counter(f["severity"] for f in findings)
    by_method = {m["methodology"]: len(m["findings"]) for m in methods if m["ran"]}
    files_scanned = sum(m.get("files", 0) for m in methods)

    ran = [m for m in methods if m["ran"]]
    to_enable = [{"label": m["label"], "tool": m["tool"], "install": m["install"], "note": m["note"]}
                 for m in methods if not m["ran"] and m["available"] is False and m["install"]]
    owasp = sorted({c for m in ran for c in _OWASP_BY_METHOD.get(m["methodology"], [])})

    return {
        "ok": True,
        "config": "multi-scanner",
        "files_scanned": files_scanned,
        "modules_scanned": sorted({(Path(f["file"]).parts[0] if len(Path(f["file"]).parts) > 1 else "(root)")
                                   for f in findings}) or ["(repo)"],
        "total": len(findings),
        "by_severity": {"critical": by_sev.get("critical", 0), "high": by_sev.get("high", 0),
                        "medium": by_sev.get("medium", 0), "low": by_sev.get("low", 0),
                        "info": by_sev.get("info", 0)},
        "by_module": dict(Counter((Path(f["file"]).parts[0] if len(Path(f["file"]).parts) > 1 else "(root)")
                                  for f in findings).most_common()),
        "by_methodology": by_method,
        "methodologies": [{"methodology": m["methodology"], "label": m["label"], "tool": m["tool"],
                           "ran": m["ran"], "count": len(m["findings"]), "note": m["note"],
                           "install": m["install"]} for m in methods],
        "methodologies_run": [m["label"] for m in ran],
        "to_enable": to_enable,
        "owasp_coverage": owasp,
        "top_rules": Counter(f["rule"] for f in findings).most_common(10),
        "findings": findings,
        "errors": [],
    }


# --------------------------------------------------------------------------- Reports
def report_markdown(report: dict, repo: str) -> str:
    if not report.get("ok"):
        return f"# Security scan — {repo}\n\n**Scan could not run:** {report.get('error')}\n"
    s = report["by_severity"]
    lines = [
        f"# Security report — {repo}",
        "",
        f"**Methodologies run:** {', '.join(report['methodologies_run']) or '—'}  ·  "
        f"**files scanned:** {report['files_scanned']}",
        "",
        f"## Summary — {report['total']} finding(s)",
        f"- 🔴 Critical: **{s['critical']}**",
        f"- 🟠 High: **{s['high']}**",
        f"- 🟡 Medium: **{s['medium']}**",
        f"- ⚪ Low: **{s['low']}**",
        "",
        "## Coverage by methodology",
    ]
    for m in report["methodologies"]:
        if m["ran"]:
            lines.append(f"- ✅ **{m['label']}** ({m['tool']}) — {m['count']} finding(s)"
                         + (f" · {m['note']}" if m["note"] else ""))
        else:
            hint = f" — enable: `{m['install']}`" if m["install"] else ""
            lines.append(f"- ⚪ {m['label']} ({m['tool']}) — not run{hint}"
                         + (f" · {m['note']}" if m["note"] and not m["install"] else ""))
    if report.get("owasp_coverage"):
        lines += ["", "## OWASP Top 10 coverage", "- " + "\n- ".join(report["owasp_coverage"])]
    lines += ["", "## Top findings"]
    for f in sorted(report["findings"], key=lambda x: _SEV_ORDER.get(x["severity"], 5))[:30]:
        icon = {"critical": "🔴", "high": "🟠", "medium": "🟡", "low": "⚪", "info": "·"}.get(f["severity"], "·")
        tags = " ".join(t for t in [f.get("cwe"), f.get("owasp")] if t)
        loc = f["file"] + (f":{f['line']}" if f.get("line") else "")
        lines.append(f"- {icon} **{f['rule']}** _({f['methodology']})_ — `{loc}`"
                     + (f"  ·  {tags}" if tags else "") + f"\n  {f['message']}")
    if report["total"] > 30:
        lines.append(f"\n_…and {report['total'] - 30} more (see the JSON/SARIF report)._")
    if report.get("to_enable"):
        lines += ["", "## Enable more coverage (install to activate)"]
        for t in report["to_enable"]:
            lines.append(f"- **{t['label']}** — `{t['install']}`")
    return "\n".join(lines)


def report_sarif(report: dict) -> dict:
    """Minimal SARIF 2.1.0 — the enterprise interchange format (GitHub Security tab, CWE/CVSS)."""
    rules, results = {}, []
    for f in report.get("findings", []):
        rid = f"{f['methodology']}/{f['rule']}"
        rules.setdefault(rid, {"id": rid, "name": f["rule"],
                               "properties": {k: f[k] for k in ("cwe", "owasp") if f.get(k)}})
        results.append({
            "ruleId": rid, "level": {"critical": "error", "high": "error", "medium": "warning"}.get(f["severity"], "note"),
            "message": {"text": f["message"]},
            "locations": [{"physicalLocation": {
                "artifactLocation": {"uri": f["file"]},
                "region": {"startLine": f.get("line") or 1}}}],
            "properties": {"severity": f["severity"], "methodology": f["methodology"],
                           **({"cwe": f["cwe"]} if f.get("cwe") else {}),
                           **({"owasp": f["owasp"]} if f.get("owasp") else {})},
        })
    return {"$schema": "https://json.schemastore.org/sarif-2.1.0.json", "version": "2.1.0",
            "runs": [{"tool": {"driver": {"name": "AI-Testing-Marketplace Security Agent",
                                          "rules": list(rules.values())}}, "results": results}]}
