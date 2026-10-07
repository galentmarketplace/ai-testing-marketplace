"""Go code coverage — runs the repo's own tests with `-coverprofile` and reports real coverage.

Deterministic infrastructure (no LLM): `go test ./... -coverprofile -covermode=atomic`, then
`go tool cover -func` for per-function detail. Emits the standard LCOV + Cobertura formats (via
reports.py) so IDEs/CI/Sonar consume it. Degrades gracefully: no `go` → a clear 'install' note;
not a Go repo → 'not applicable'. Never crashes a run.
"""
import shutil
import subprocess
from pathlib import Path

from .. import sandbox
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
                           cwd=str(mod_dir), capture_output=True, text=True, timeout=timeout,
                           env=sandbox.child_env())
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


def _file_table(files: list[dict], limit: int = 40) -> list[str]:
    out = []
    for f in sorted(files, key=lambda x: x["pct"])[:limit]:
        bar = "\u2588" * int(f["pct"] // 10) + "\u2591" * (10 - int(f["pct"] // 10))
        out.append(f"- `{f['file']}` {bar} **{f['pct']:.1f}%** ({f['covered']}/{f['lines']} lines)")
    return out


def _func_pct(rep: dict, name: str, file: str) -> float | None:
    """The coverage of one function in a report, matched on name + file."""
    for fn in rep.get("funcs") or []:
        if fn.get("func") == name and Path(str(fn.get("file", ""))).name == Path(str(file)).name:
            return fn.get("pct")
    return None


def functions_fixed(rep: dict) -> list[dict]:
    """Functions that were at 0% before and have coverage after — from the MEASUREMENTS.

    The generator used to report this from the model's own `covers` field, which the model
    sometimes omits entirely: a run that genuinely covered four functions described itself
    as "for 0 function(s)", and the pull request said it covered 0. What a function's
    coverage is now is measurable, so measure it.
    """
    after = rep.get("after") or {}
    if not after.get("ok"):
        return []
    out = []
    for u in rep.get("uncovered_funcs") or []:
        pct = _func_pct(after, u["func"], u["file"])
        if pct:
            out.append({"func": u["func"], "file": u["file"], "line": u.get("line"), "pct": pct})
    return out


def report_markdown(rep: dict, repo: str, min_pct: float) -> str:
    """The coverage artifact.

    When a run only MEASURES, this is a gap report. When the run also FIXED the gap — the Go
    test generator wrote tests, they compiled and passed, and coverage was re-measured — the
    report must show the fix: before, after, and which functions moved. Showing only the
    before state made a successful run read like a failed one.
    """
    if not rep.get("ok"):
        return (f"# Go coverage \u2014 {repo}\n\n**Not measured:** {rep.get('error')}\n"
                + (f"\nEnable: `{rep['install']}`\n" if rep.get("install") else ""))
    before = rep["total_pct"]
    after = rep.get("after") or {}
    prop = rep.get("proposed_tests") or {}
    final_rep = after if after.get("ok") else rep
    final = final_rep.get("total_pct", before)
    icon = "\u2705" if final >= min_pct else "\U0001f534"
    tests_ok = final_rep.get("tests_passed", rep.get("tests_passed"))

    lines = [f"# Go coverage report \u2014 {repo}", ""]
    if after.get("ok"):
        lines += [f"{icon} **Coverage {before:.1f}% \u2192 {final:.1f}%**  "
                  f"({final - before:+.1f} points, threshold {min_pct:.0f}%)",
                  "",
                  f"- Statements covered: {rep['statements_covered']}/{rep['statements']} "
                  f"\u2192 {final_rep['statements_covered']}/{final_rep['statements']}",
                  f"- Test suite: {'passed' if tests_ok else 'FAILED'} "
                  f"(generated tests included)"]
    else:
        lines += [f"{icon} **Total coverage: {final:.1f}%**  (threshold {min_pct:.0f}%) \u00b7 "
                  f"{final_rep['statements_covered']}/{final_rep['statements']} statements \u00b7 "
                  f"tests {'passed' if tests_ok else 'FAILED'}"]

    # ---- what the run changed ----
    if prop and not prop.get("proposed"):
        lines += ["", "## Test generation attempted \u2014 nothing usable produced", "",
                  f"\u26a0\ufe0f The {len(rep.get('uncovered_funcs') or [])} function(s) below are "
                  f"still uncovered: {prop.get('note') or 'no test file was generated'}.",
                  "", "This is a failed attempt, not a decision that the gap is acceptable."]
    if prop.get("proposed"):
        verified = prop.get("verified")
        lines += ["", "## What this run changed", "",
                  f"{'\u2705' if verified else '\u26a0\ufe0f'} "
                  f"**{prop['proposed']} test file(s) generated** for "
                  f"{len(functions_fixed(rep)) or len(prop.get('covers', []))} "
                  f"previously-uncovered function(s) \u2014 "
                  + ("compiled and passed, coverage re-measured below."
                     if verified else f"{prop.get('note', 'not verified')}.")]
        fixed = functions_fixed(rep)
        if fixed:
            lines += ["", "| Function | Source | Before | After |", "|---|---|---|---|"]
            for u in fixed[:30]:
                lines.append(f"| `{u['func']}` | `{u['file']}:{u['line']}` | 0.0% | **{u['pct']:.1f}%** |")
        for s_ in (prop.get("skipped") or [])[:10]:
            lines.append(f"- not tested \u2014 `{s_.get('func')}`: {s_.get('reason')}")

    lines += ["", "## Coverage by file", *_file_table(final_rep.get("files") or [])]

    remaining = final_rep.get("uncovered_funcs") or []
    if remaining:
        heading = ("Still uncovered" if after.get("ok") else "Uncovered functions")
        lines += ["", f"## {heading} ({len(remaining)}) \u2014 candidates for new tests"]
        for u in remaining[:30]:
            lines.append(f"- `{u['func']}` \u2014 `{u['file']}:{u['line']}`")
    elif after.get("ok"):
        lines += ["", "## Still uncovered", "", "None \u2014 every function reported by "
                  "`go tool cover -func` now has coverage."]

    lines += ["", "_Standard exports: `coverage.lcov` (VS Code / Codecov / Sonar) \u00b7 "
              "`cobertura.xml` (Jenkins / GitLab)._"]
    return "\n".join(lines)
