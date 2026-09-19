"""Run a repo's REAL unit tests (any stack) and parse the result.

Detects the test setup — Node (npm test / jest / mocha / vitest / ava) or Python
(pytest / unittest) — installs deps best-effort, runs the suite, and extracts pass/fail
counts. Returns a structured result, including a clear `no_tests` signal when the repo
has no unit tests to run (so the pipeline reports that instead of looping).
"""
import json
import os
import re
import subprocess
from pathlib import Path

_NODE_BIN = "/opt/homebrew/opt/node@20/bin"


def _bin(name: str) -> str:
    p = Path(_NODE_BIN) / name
    return str(p) if p.exists() else name


def _node_env() -> dict:
    return {**os.environ, "PATH": _NODE_BIN + os.pathsep + os.environ.get("PATH", ""), "CI": "1"}


def _run(cmd, cwd, timeout, env=None):
    try:
        p = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, timeout=timeout, env=env)
        return p.returncode, (p.stdout or "") + "\n" + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout}s"
    except Exception as exc:
        return 1, f"failed to run {cmd[0]}: {exc}"


def _parse(out: str):
    """Best-effort pass/fail extraction across common runners. Returns (passed, failed) or None."""
    m = re.search(r"Tests?:\s+(?:(\d+)\s+failed,\s+)?(\d+)\s+passed", out)      # jest / vitest
    if m:
        return int(m.group(2)), int(m.group(1) or 0)
    pas, fail = re.search(r"(\d+)\s+passing", out), re.search(r"(\d+)\s+failing", out)  # mocha
    if pas:
        return int(pas.group(1)), int(fail.group(1) if fail else 0)
    pp, pf = re.search(r"(\d+)\s+passed", out), re.search(r"(\d+)\s+failed", out)       # pytest
    if pp or pf:
        return int(pp.group(1) if pp else 0), int(pf.group(1) if pf else 0)
    return None


def _fail_lines(out: str, limit: int = 8) -> list:
    hits = [ln.strip() for ln in out.splitlines()
            if re.search(r"(✕|✗|FAIL|failing|AssertionError|Error:|✖)", ln)][:limit]
    return [{"test": "unit", "error": h[:300]} for h in hits]


def run_unit(path: str, timeout: int = 600) -> dict:
    root = Path(path)
    if not root.exists():
        return {"ok": False, "error": f"repo path not found: {path}"}

    pkg = root / "package.json"
    if pkg.exists():
        try:
            p = json.loads(pkg.read_text())
        except Exception:
            p = {}
        scripts = p.get("scripts", {})
        deps = {**p.get("dependencies", {}), **p.get("devDependencies", {})}
        test_script = scripts.get("test", "")
        has_test_script = bool(test_script.strip()) and "no test specified" not in test_script
        fw = next((f for f in ("jest", "vitest", "mocha", "ava") if f in deps), None)
        if not has_test_script and not fw:
            return {"ok": True, "no_tests": True, "framework": "node",
                    "message": "No `test` script and no test framework (jest/mocha/vitest) in package.json — nothing to run."}
        env = _node_env()
        _run([_bin("npm"), "install", "--no-audit", "--no-fund", "--loglevel=error"], root, min(timeout, 480), env)
        cmd = [_bin("npm"), "test", "--silent"] if has_test_script else [_bin("npx"), fw]
        code, out = _run(cmd, root, timeout, env)
        pf = _parse(out)
        passed, failed = pf if pf else ((1, 0) if code == 0 else (0, 1))
        return {"ok": True, "framework": "node/" + (fw or "npm test"), "passed": passed,
                "failed": failed, "exit": code, "failures": _fail_lines(out), "tail": out[-1500:]}

    # Python
    py_tests = list(root.glob("test_*.py")) + list(root.glob("**/test_*.py"))[:1] + \
        list(root.glob("tests/**/*.py"))[:1]
    if (root / "pyproject.toml").exists() or (root / "setup.py").exists() or py_tests:
        code, out = _run(["python3", "-m", "pytest", "-q"], root, timeout, os.environ.copy())
        if "No module named pytest" in out:
            return {"ok": True, "no_tests": True, "framework": "python",
                    "message": "pytest not available to run the Python tests."}
        pf = _parse(out)
        if not pf and "no tests ran" in out.lower():
            return {"ok": True, "no_tests": True, "framework": "python",
                    "message": "pytest found no tests to run."}
        passed, failed = pf if pf else ((1, 0) if code == 0 else (0, 1))
        return {"ok": True, "framework": "python/pytest", "passed": passed, "failed": failed,
                "exit": code, "failures": _fail_lines(out), "tail": out[-1500:]}

    return {"ok": True, "no_tests": True, "framework": "unknown",
            "message": "No recognized unit-test setup (Node or Python) in this repo."}


def report_markdown(r: dict, repo: str) -> str:
    if not r.get("ok"):
        return f"# Unit tests — {repo}\n\n**Could not run:** {r.get('error')}\n"
    if r.get("no_tests"):
        return (f"# Unit tests — {repo}\n\n**No unit tests were run.**\n\n{r.get('message', '')}\n\n"
                f"_Detected stack: {r.get('framework', 'unknown')}._ To get unit tests here, add a test "
                f"framework/suite to the repo (or use a generation-capable model to author them).")
    total = r["passed"] + r["failed"]
    lines = [f"# Unit test report — {repo}", "",
             f"**Runner:** {r['framework']} · **result:** {'✅ all passing' if r['failed'] == 0 else '❌ failures'}",
             "", f"- Passed: **{r['passed']}**", f"- Failed: **{r['failed']}**", f"- Total: **{total}**"]
    if r.get("failures"):
        lines += ["", "## Failures"] + [f"- `{f['error']}`" for f in r["failures"]]
    return "\n".join(lines)
