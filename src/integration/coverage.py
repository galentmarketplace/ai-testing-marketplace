"""Multi-language code coverage — one contract, several toolchains.

Coverage used to be Go-only, which made the whole track useless on a JavaScript or Python
codebase. The measurement is not what differs between languages; only the command is. So
this dispatches to the right tool and normalises everything through LCOV, which jest,
vitest, nyc/c8 and coverage.py all already emit.

The return shape is deliberately identical to `go_coverage.run_go_coverage`, so the agent,
the gate and the standard exports (LCOV + Cobertura) work unchanged for every language.

Honesty rules, the same as everywhere else in the platform:
  * a repo with no tests reports `not_applicable`, never 0% (which reads like a failure
    to cover rather than nothing to measure)
  * a toolchain that is missing says so and names the fix
  * `tests_passed` reflects the real exit code, so a green coverage number from a red
    suite cannot be mistaken for success
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from .. import sandbox
from . import go_coverage, reports

TIMEOUT = 900

# Directories that hold other projects or build output, never this repo's own source.
_SKIP_DIRS = {"repos", "node_modules", "vendor", ".venv", "venv", "generated",
              "dist", "build", ".git", "__pycache__", "e2e-runner", "backups",
              "coverage", "playwright-report", "test-results", ".storybook",
              "storybook-static", ".next", "out", "public"}

# Build/tooling config is not application source; counting it makes every project look
# under-instrumented for files no coverage tool would ever report on.
_CONFIG_SUFFIXES = (".config.js", ".config.ts", ".config.mjs", ".config.cjs",
                    ".conf.js", ".eslintrc.js", "rc.js", "setup.py", "conftest.py")


# ------------------------------------------------------------------ detection
def detect_language(path: str | Path) -> str:
    root = Path(path)
    # Bounded, and skipping clone/vendor directories: an unbounded rglob finds a go.mod
    # inside a cached third-party repo and misreports the whole workspace as Go.
    if (root / "go.mod").exists() or any(
            (d / "go.mod").exists() for d in root.iterdir()
            if d.is_dir() and d.name not in _SKIP_DIRS):
        return "go"
    pj = root / "package.json"
    if pj.is_file():
        try:
            data = json.loads(pj.read_text())
        except Exception:
            data = {}
        deps = {**(data.get("dependencies") or {}), **(data.get("devDependencies") or {})}
        if any(k in deps for k in ("jest", "vitest", "@vitest/coverage-v8", "nyc", "c8")):
            return "node"
        if (data.get("scripts") or {}).get("test"):
            return "node"
    if (root / "pyproject.toml").is_file() or (root / "setup.py").is_file() \
            or (root / "pytest.ini").is_file() or list(root.glob("test_*.py")) \
            or (root / "tests").is_dir():
        return "python"
    return "unknown"


def _run(cmd: list[str], cwd: Path, timeout: int = TIMEOUT) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True,
                           timeout=timeout, env=sandbox.child_env())
    except subprocess.TimeoutExpired:
        return 124, f"`{' '.join(cmd[:3])}` timed out after {timeout}s"
    except FileNotFoundError:
        return 127, f"{cmd[0]} not found"
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def _lcov_from(root: Path) -> str:
    """Find the LCOV file the tool just wrote, wherever it chose to put it."""
    for rel in ("coverage/lcov.info", "coverage/lcov.dat", "lcov.info",
                "coverage.lcov", "coverage/coverage.lcov"):
        p = root / rel
        if p.is_file() and p.stat().st_size:
            return p.read_text(errors="replace")
    for p in root.rglob("lcov.info"):
        if "node_modules" not in p.parts and p.stat().st_size:
            return p.read_text(errors="replace")
    return ""


# ------------------------------------------------------------------ node
def _node(root: Path) -> dict:
    if not shutil.which("npm"):
        return {"ok": False, "error": "npm not installed", "install": "install Node 20+"}
    try:
        pkg = json.loads((root / "package.json").read_text())
    except Exception as exc:
        return {"ok": False, "error": f"unreadable package.json: {exc}"}
    scripts = pkg.get("scripts") or {}
    deps = {**(pkg.get("dependencies") or {}), **(pkg.get("devDependencies") or {})}

    # An EMPTY node_modules directory is not an install. Checking only for the path lets a
    # leftover directory skip the install and the run then dies on "jest: command not found".
    mods = root / "node_modules"
    if not (mods.is_dir() and any(mods.iterdir())):
        rc, out = _run(["npm", "ci"] if (root / "package-lock.json").is_file() else ["npm", "install"], root)
        if rc != 0:
            return {"ok": False, "error": f"dependency install failed: {out[-400:]}"}

    # A script the repo already defines is preferred: it encodes the project's own config.
    cov_script = next((s for s in ("test:coverage", "test.coverage", "coverage") if s in scripts), None)
    if cov_script:
        cmd = ["npm", "run", cov_script]
    elif "vitest" in deps:
        cmd = ["npx", "vitest", "run", "--coverage", "--coverage.reporter=lcov"]
    elif "jest" in deps:
        cmd = ["npx", "jest", "--coverage", "--coverageReporters=lcov", "--ci"]
    else:
        return {"ok": False, "not_applicable": True,
                "error": "no jest/vitest and no coverage script — nothing to measure"}

    rc, out = _run(cmd, root)
    text = _lcov_from(root)
    if not text:
        return {"ok": False, "error": ("the test run produced no LCOV report. "
                                       f"Command: {' '.join(cmd)}. Output: {out[-400:]}")}
    cov = reports.parse_lcov(text, root=str(root))
    return {"ok": True, "tool": " ".join(cmd), "language": "node", "module_dir": str(root),
            "tests_passed": rc == 0, "test_output_tail": out[-1200:], **cov,
            "funcs": [], "uncovered_funcs": []}


# ------------------------------------------------------------------ python
def _python(root: Path) -> dict:
    py = shutil.which("python3") or shutil.which("python")
    if not py:
        return {"ok": False, "error": "python not installed"}
    out_file = root / "coverage.lcov"
    rc, out = _run([py, "-m", "pytest", "--cov=.", f"--cov-report=lcov:{out_file}",
                    "--cov-report=term", "-q"], root)
    if "No module named pytest" in out:
        return {"ok": False, "error": "pytest is not installed in this environment",
                "install": "pip install pytest pytest-cov"}
    if "unrecognized arguments: --cov" in out or "no such option" in out.lower():
        return {"ok": False, "error": "pytest-cov is not installed",
                "install": "pip install pytest-cov"}
    text = out_file.read_text(errors="replace") if out_file.is_file() else _lcov_from(root)
    if not text:
        return {"ok": False, "error": f"no LCOV report produced. Output: {out[-400:]}"}
    cov = reports.parse_lcov(text, root=str(root))
    return {"ok": True, "tool": "pytest --cov", "language": "python", "module_dir": str(root),
            "tests_passed": rc == 0, "test_output_tail": out[-1200:], **cov,
            "funcs": [], "uncovered_funcs": []}



_SRC_EXT = {"node": {".js", ".jsx", ".ts", ".tsx", ".vue", ".svelte"},
            "python": {".py"}, "go": {".go"}}
_TESTISH = ("test", "spec", "__mocks__", "__tests__", "conftest", "setuptests")


def _source_files(root: Path, lang: str) -> set[str]:
    """Candidate source files, excluding tests, mocks and vendored trees."""
    exts = _SRC_EXT.get(lang, set())
    out = set()
    for p in root.rglob("*"):
        if not p.is_file() or p.suffix not in exts:
            continue
        rel = p.relative_to(root)
        if any(part in _SKIP_DIRS for part in rel.parts):
            continue
        low = str(rel).lower()
        if any(t in low for t in _TESTISH):
            continue
        if low.endswith(_CONFIG_SUFFIXES):
            continue
        out.add(str(rel))
    return out


def annotate_scope(rep: dict, root: Path, lang: str) -> dict:
    """Record how much of the codebase the percentage actually covers.

    A tool that only instruments files its tests import will happily report 100% while
    ignoring every untested file. The headline number is then true and useless. Reporting
    the unmeasured count turns that from a hidden bias into a visible fact the gate can act on.
    """
    if not rep.get("ok"):
        return rep
    src = _source_files(root, lang)
    measured = {f["file"].lstrip("./") for f in rep.get("files", [])}

    def _is_measured(rel: str) -> bool:
        """Go reports package-qualified paths (github.com/org/repo/mux.go) while the source
        walk yields repo-relative ones, so compare by path suffix rather than equality."""
        if rel in measured:
            return True
        return any(m == rel or m.endswith("/" + rel) or rel.endswith("/" + m) for m in measured)

    unmeasured = sorted(f for f in src if not _is_measured(f))
    hit = len(src) - len(unmeasured)
    rep["files_measured"] = len(measured)
    rep["files_in_repo"] = len(src)
    rep["files_unmeasured"] = len(unmeasured)
    rep["unmeasured_sample"] = unmeasured[:15]
    if src:
        rep["scope_pct"] = round(hit * 100 / len(src), 1)
        # The number a reviewer should actually trust: coverage scaled by how much of the
        # codebase was even looked at.
        rep["effective_pct"] = round(rep.get("total_pct", 0) * rep["scope_pct"] / 100, 1)
        if rep["files_unmeasured"]:
            rep["scope_warning"] = (
                f"{rep['files_unmeasured']} of {len(src)} source files were not instrumented, "
                f"so {rep.get('total_pct')}% covers only {rep['scope_pct']}% of the codebase "
                f"(effective {rep['effective_pct']}%).")
    return rep


# ------------------------------------------------------------------ entry point
def measure(path: str | Path, language: str = "auto") -> dict:
    """Measure coverage for a repository in whatever language it is written in."""
    root = Path(path)
    if not root.is_dir():
        return {"ok": False, "error": f"repo path not found: {path}"}

    lang = language if language and language != "auto" else detect_language(root)
    if lang == "go":
        rep = go_coverage.run_go_coverage(str(root))
        rep.setdefault("language", "go")
        return annotate_scope(rep, root, "go")
    if lang == "node":
        return annotate_scope(_node(root), root, "node")
    if lang == "python":
        return annotate_scope(_python(root), root, "python")
    return {"ok": False, "not_applicable": True, "language": lang,
            "error": "could not identify a supported test setup (Go, Node or Python)"}
