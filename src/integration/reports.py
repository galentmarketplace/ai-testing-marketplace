"""Standard report formats — the interchange formats CI servers, IDEs and enterprise tools consume.

Standardisation rule: every runner/agent emits its native report PLUS a standard one:
  * test runs      → JUnit XML   (Jenkins, GitHub, Azure DevOps all ingest it natively)
  * code coverage  → LCOV + Cobertura XML (VS Code Test Coverage API, SonarQube, Codecov, Jenkins)
  * security       → SARIF 2.1.0 (see security_scan.report_sarif)
  * SBOM           → CycloneDX   (see security_scan.scan_sbom)
  * test cases     → YAML intent DSL (see functional_case_agent)
"""
import time
from collections import defaultdict
from xml.sax.saxutils import escape, quoteattr


# ----------------------------------------------------------------------------- JUnit XML
def junit_xml(result: dict, name: str | None = None) -> str:
    """RunResult dict → JUnit XML. Passed cases are synthesised (the runner reports counts), failed
    ones carry the real test title + error so CI shows exactly what broke."""
    suite = name or result.get("suite", "suite")
    passed, failed = int(result.get("passed", 0)), int(result.get("failed", 0))
    fails = list(result.get("failures") or [])
    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           f'<testsuites name={quoteattr(suite)} tests="{passed + failed}" failures="{failed}">',
           f'  <testsuite name={quoteattr(suite)} tests="{passed + failed}" failures="{failed}" '
           f'errors="0" skipped="0" timestamp={quoteattr(time.strftime("%Y-%m-%dT%H:%M:%S"))}>']
    for i in range(passed):
        out.append(f'    <testcase classname={quoteattr(suite)} name={quoteattr(f"{suite} case #{i + 1}")}/>')
    for i in range(failed):
        f = fails[i] if i < len(fails) else {}
        title = f.get("test") or f"{suite} failure #{i + 1}"
        err = (f.get("error") or "failed")
        out.append(f'    <testcase classname={quoteattr(suite)} name={quoteattr(title)}>')
        out.append(f'      <failure message={quoteattr(err.splitlines()[0][:200])}>{escape(err)}</failure>')
        out.append('    </testcase>')
    out += ['  </testsuite>', '</testsuites>', '']
    return "\n".join(out)


# ----------------------------------------------------------------------------- Go coverprofile → line coverage
def parse_coverprofile(text: str) -> dict:
    """Go `-coverprofile` → {file: {line: hits}} plus statement-weighted totals (how `go tool cover` counts).
    Format: `path/file.go:startLine.startCol,endLine.endCol numStatements count`."""
    lines: dict = defaultdict(dict)
    stmts_total = stmts_covered = 0
    for raw in text.splitlines():
        if not raw or raw.startswith("mode:"):
            continue
        try:
            loc, n, cnt = raw.rsplit(" ", 2)
            path, rng = loc.rsplit(":", 1)
            start, end = rng.split(",")
            s_line, e_line = int(start.split(".")[0]), int(end.split(".")[0])
            n, cnt = int(n), int(cnt)
        except ValueError:
            continue
        stmts_total += n
        if cnt > 0:
            stmts_covered += n
        for ln in range(s_line, e_line + 1):
            lines[path][ln] = max(lines[path].get(ln, 0), cnt)
    files = []
    for path, m in sorted(lines.items()):
        total, covered = len(m), sum(1 for h in m.values() if h > 0)
        files.append({"file": path, "lines": total, "covered": covered,
                      "pct": round(100 * covered / total, 1) if total else 0.0})
    total_pct = round(100 * stmts_covered / stmts_total, 1) if stmts_total else 0.0
    return {"files": files, "lines": dict(lines), "total_pct": total_pct,
            "statements": stmts_total, "statements_covered": stmts_covered}


def parse_cover_func(text: str) -> dict:
    """`go tool cover -func=cov.out` → per-function %, the uncovered list, and the total line."""
    funcs, total = [], None
    for raw in text.splitlines():
        parts = [p for p in raw.split("\t") if p]
        if len(parts) < 3:
            continue
        pct = parts[-1].strip().rstrip("%")
        try:
            pctf = float(pct)
        except ValueError:
            continue
        if parts[0].startswith("total:"):
            total = pctf
            continue
        loc, fn = parts[0], parts[1]
        file, _, line = loc.rpartition(":")
        funcs.append({"file": file, "line": int(line) if line.isdigit() else None, "func": fn, "pct": pctf})
    return {"funcs": funcs, "total_pct": total,
            "uncovered": [f for f in funcs if f["pct"] == 0.0]}


def lcov(cov: dict) -> str:
    """{file:{line:hits}} → LCOV (what VS Code's Test Coverage API, Codecov and SonarQube read)."""
    out = []
    for path, m in sorted(cov["lines"].items()):
        out.append("TN:")
        out.append(f"SF:{path}")
        for ln in sorted(m):
            out.append(f"DA:{ln},{m[ln]}")
        out.append(f"LF:{len(m)}")
        out.append(f"LH:{sum(1 for h in m.values() if h > 0)}")
        out.append("end_of_record")
    return "\n".join(out) + "\n"


def cobertura_xml(cov: dict, source_root: str = ".") -> str:
    """{file:{line:hits}} → minimal Cobertura XML (Jenkins Coverage plugin, GitLab, Azure DevOps)."""
    rate = cov["total_pct"] / 100
    ln_total = sum(len(m) for m in cov["lines"].values())
    ln_cov = sum(1 for m in cov["lines"].values() for h in m.values() if h > 0)
    out = ['<?xml version="1.0" ?>',
           f'<coverage line-rate="{rate:.4f}" branch-rate="0" lines-covered="{ln_cov}" lines-valid="{ln_total}" '
           f'version="1.0" timestamp="{int(time.time())}">',
           f'  <sources><source>{escape(source_root)}</source></sources>', '  <packages>']
    by_pkg: dict = defaultdict(list)
    for path in sorted(cov["lines"]):
        by_pkg[path.rsplit("/", 1)[0] if "/" in path else "."].append(path)
    for pkg, paths in by_pkg.items():
        out.append(f'    <package name={quoteattr(pkg)} line-rate="0" branch-rate="0"><classes>')
        for path in paths:
            m = cov["lines"][path]
            r = sum(1 for h in m.values() if h > 0) / len(m) if m else 0
            out.append(f'      <class name={quoteattr(path.rsplit("/", 1)[-1])} filename={quoteattr(path)} '
                       f'line-rate="{r:.4f}" branch-rate="0"><methods/><lines>')
            for ln in sorted(m):
                out.append(f'        <line number="{ln}" hits="{m[ln]}"/>')
            out.append('      </lines></class>')
        out.append('    </classes></package>')
    out += ['  </packages>', '</coverage>', '']
    return "\n".join(out)
