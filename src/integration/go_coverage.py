"""Go code coverage — runs the repo's own tests with `-coverprofile` and reports real coverage.

Deterministic infrastructure (no LLM): `go test ./... -coverprofile -covermode=atomic`, then
`go tool cover -func` for per-function detail. Emits the standard LCOV + Cobertura formats (via
reports.py) so IDEs/CI/Sonar consume it. Degrades gracefully: no `go` → a clear 'install' note;
not a Go repo → 'not applicable'. Never crashes a run.
"""
import shutil
import subprocess
from pathlib import Path

from . import reports


def available() -> bool:
    return bool(shutil.which("go"))


def is_go_repo(path: str) -> bool:
    root = Path(path)
    return (root / "go.mod").exists() or any(root.rglob("go.mod"))


def run_go_coverage(path: str, timeout: int = 600) -> dict:
    root = Path(path)
    if not root.exists():
        return {"ok": False, "error": f"repo path not found: {path}"}
    if not is_go_repo(path):
        return {"ok": False, "not_applicable": True, "error": "no go.mod — not a Go repository"}
    if not available():
        return {"ok": False, "error": "go toolchain not installed (brew install go)", "install": "brew install go"}
    mod_dir = root if (root / "go.mod").exists() else next(root.rglob("go.mod")).parent
    prof = mod_dir / "coverage.out"
    try:
        t = subprocess.run(["go", "test", "./...", "-count=1", "-covermode=atomic", f"-coverprofile={prof}"],
                           cwd=str(mod_dir), capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"go test timed out after {timeout}s"}
    except Exception as exc:
        return {"ok": False, "error": f"go test failed to run: {exc}"}
    if not prof.exists():
        return {"ok": False, "error": (t.stderr or t.stdout or "no coverprofile produced")[-600:]}
    cov = reports.parse_coverprofile(prof.read_text())
    func_txt = ""
    try:
        f = subprocess.run(["go", "tool", "cover", f"-func={prof}"], cwd=str(mod_dir),
                           capture_output=True, text=True, timeout=120)
        func_txt = f.stdout or ""
    except Exception:
        pass
    funcs = reports.parse_cover_func(func_txt) if func_txt else {"funcs": [], "total_pct": None, "uncovered": []}
    return {
        "ok": True, "tool": "go test -coverprofile", "module_dir": str(mod_dir),
        "tests_passed": t.returncode == 0,
        "total_pct": funcs["total_pct"] if funcs["total_pct"] is not None else cov["total_pct"],
        "statements": cov["statements"], "statements_covered": cov["statements_covered"],
        "files": cov["files"], "lines": cov["lines"],
        "funcs": funcs["funcs"], "uncovered_funcs": funcs["uncovered"][:50],
        "test_output_tail": (t.stdout or "")[-1200:],
    }


def report_markdown(rep: dict, repo: str, min_pct: float) -> str:
    if not rep.get("ok"):
        return (f"# Go coverage — {repo}\n\n**Not measured:** {rep.get('error')}\n"
                + (f"\nEnable: `{rep['install']}`\n" if rep.get("install") else ""))
    tot = rep["total_pct"]
    icon = "✅" if tot >= min_pct else "🔴"
    lines = [f"# Go coverage report — {repo}", "",
             f"{icon} **Total coverage: {tot:.1f}%**  (threshold {min_pct:.0f}%) · "
             f"{rep['statements_covered']}/{rep['statements']} statements · tests {'passed' if rep['tests_passed'] else 'FAILED'}",
             "", "## Coverage by file"]
    for f in sorted(rep["files"], key=lambda x: x["pct"])[:40]:
        bar = "█" * int(f["pct"] // 10) + "░" * (10 - int(f["pct"] // 10))
        lines.append(f"- `{f['file']}` {bar} **{f['pct']:.1f}%** ({f['covered']}/{f['lines']} lines)")
    if rep["uncovered_funcs"]:
        lines += ["", f"## Uncovered functions ({len(rep['uncovered_funcs'])}) — candidates for new tests"]
        for u in rep["uncovered_funcs"][:30]:
            lines.append(f"- `{u['func']}` — `{u['file']}:{u['line']}`")
    lines += ["", "_Standard exports: `coverage.lcov` (VS Code / Codecov / Sonar) · `cobertura.xml` (Jenkins / GitLab)._"]
    return "\n".join(lines)
