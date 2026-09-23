"""Go Test Generation agent — phase 2 of coverage (the EarlyAI pattern): propose unit tests for the
functions the coverage run found at 0%, grounded in their ACTUAL source.

Execution-in-the-loop: when `go` is available the proposals are dropped into the cloned repo, compiled
and run (`go test ./...`); anything that doesn't compile/pass is withdrawn, and coverage is re-measured
so the gate sees the REAL post-generation number. Without `go` the proposals are still written as
artifacts but marked unverified — never presented as green. Generated files never touch the user's
working tree: they go to generated/coverage/tests/ (and only into OUR clone for verification).
"""
import json
import shutil
import subprocess
from pathlib import Path

from .. import runctx
from ..config import GENERATED_DIR
from ..integration import go_coverage as gocov
from ..llm import call_llm_json
from ..mocks import MOCK_RESPONSES
from ..state import PipelineState

MOCK_RESPONSES.setdefault("go_test_agent", json.dumps({
    "files": [{"package": "handler", "path": "internal/handler/handler_generated_test.go", "covers": ["handleRefund"],
               "content": "package handler\n\nimport \"testing\"\n\nfunc TestHandleRefund(t *testing.T) {\n\tcases := []struct{ name string; amount int; wantErr bool }{\n\t\t{\"refund ok\", 100, false},\n\t\t{\"negative amount\", -1, true},\n\t}\n\tfor _, c := range cases {\n\t\tt.Run(c.name, func(t *testing.T) {\n\t\t\t_, err := handleRefund(c.amount)\n\t\t\tif (err != nil) != c.wantErr { t.Fatalf(\"wantErr=%v got %v\", c.wantErr, err) }\n\t\t})\n\t}\n}\n"}],
    "skipped": [{"func": "cancelOrder", "reason": "needs a live DB handle with no visible interface to fake"}]}))

SYSTEM = """You are a senior Go engineer writing UNIT TESTS for functions that currently have 0% coverage.
Rules:
- Same package as the function (`package <name>`), file `<base>_generated_test.go` beside it. Table-driven, t.Run per case.
- Standard library only (testing + what the function's own file already imports). Use the EXACT names and
  signatures shown in the source — the code MUST compile as-is.
- Assert REAL outcomes for a happy path AND at least one edge/error case per function. Never write
  always-true assertions or tests that pass without exercising the function.
- If a function needs unavailable dependencies (DB, network, external service) and no interface/fake is
  visible in the source, DO NOT write a test for it — list it under `skipped` with the reason.
Respond ONLY with JSON:
{"files":[{"package":"<pkg>","path":"<dir>/<base>_generated_test.go","content":"<go source>","covers":["Func"]}],
 "skipped":[{"func":"<name>","reason":"<why>"}]}"""


def _func_source(root: Path, rel: str, line: int | None, max_lines: int = 90) -> str:
    """Package clause + imports + the function body (brace-balanced) so the LLM tests the real signature."""
    try:
        lines = (root / rel).read_text(errors="ignore").splitlines()
    except Exception:
        return ""
    head = [ln for ln in lines[:60] if ln.startswith("package ")][:1]
    imp, grab = [], False
    for ln in lines[:80]:
        if ln.startswith("import"):
            grab = True
        if grab:
            imp.append(ln)
            if ln.strip().endswith(")") or (ln.startswith("import ") and "(" not in ln):
                break
    body, depth, started = [], 0, False
    for ln in lines[max(0, (line or 1) - 1):(line or 1) - 1 + max_lines]:
        body.append(ln)
        depth += ln.count("{") - ln.count("}")
        started = started or "{" in ln
        if started and depth <= 0:
            break
    return "\n".join(head + imp + ["", *body])


def generate_go_tests(state: PipelineState) -> dict:
    rep = dict(state.get("coverage_report") or {})
    uncovered = rep.get("uncovered_funcs") or []
    if not rep.get("ok") or not uncovered:
        print("  [Go Test Gen] nothing to do — coverage not measured or no uncovered functions")
        return {}
    mock = runctx.is_mock()
    root = Path(rep.get("module_dir") or "") if not mock else None
    targets = uncovered[:8]

    ctx = [f"Repo module dir: {rep.get('module_dir')}", f"Current statement coverage: {rep.get('total_pct')}%",
           f"UNCOVERED FUNCTIONS to test ({len(targets)} of {len(uncovered)}):"]
    for u in targets:
        src = _func_source(root, u["file"], u.get("line")) if root and root.exists() else ""
        ctx.append(f"\n--- {u['file']}:{u.get('line')}  func {u['func']} ---\n{src or '(source unavailable — skip unless signature is obvious)'}")
    try:
        raw = call_llm_json("go_test_agent", SYSTEM, "\n".join(ctx), max_tokens=4000)
    except Exception as exc:
        print(f"  [Go Test Gen] generation failed ({exc})")
        return {}
    files = [f for f in (raw.get("files") or []) if f.get("content") and f.get("path", "").endswith("_test.go")]
    skipped = raw.get("skipped") or []

    out_dir = GENERATED_DIR / "coverage" / "tests"
    arts = list(state.get("coverage_artifacts", []))
    for f in files:
        dst = out_dir / f["path"]
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(f["content"])
        arts.append({"type": "coverage-tests", "path": str(dst), "tags": ["@coverage", "@generated-test"],
                     "covers": f.get("covers", [])})

    # EXECUTION-IN-THE-LOOP: verify in OUR clone, withdraw anything red, re-measure.
    verified, after, note = False, None, ""
    if not mock and files and shutil.which("go") and root and root.exists():
        placed = []
        for f in files:
            dst = root / f["path"]
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_text(f["content"])
            placed.append(dst)
        t = subprocess.run(["go", "test", "./..."], cwd=str(root), capture_output=True, text=True, timeout=600)
        if t.returncode == 0:
            verified = True
            rep2 = gocov.run_go_coverage(str(root))
            after = rep2.get("total_pct") if rep2.get("ok") else None
            note = "compiled & passed in the cloned repo; coverage re-measured"
        else:
            for p in placed:
                p.unlink(missing_ok=True)
            note = "withdrawn — generated tests did not compile/pass: " + (t.stderr or t.stdout or "")[-400:].strip()
    elif not mock and files:
        note = "unverified — `go` toolchain not installed on this host (brew install go)"
    elif mock:
        note = "mock run — proposals not executed"

    summary = {"proposed": len(files), "covers": sorted({c for f in files for c in f.get("covers", [])}),
               "skipped": skipped, "verified": verified, "coverage_after_pct": after, "note": note}
    rep["proposed_tests"] = summary
    if verified and after is not None:
        rep["total_pct_after"] = after
    print(f"  [Go Test Gen] proposed {len(files)} test file(s) for {len(summary['covers'])} function(s), "
          f"skipped {len(skipped)} · {note}" + (f" · coverage {rep.get('total_pct')}% → {after}%" if after is not None else ""))
    return {"coverage_artifacts": arts, "coverage_report": rep, "go_test_proposals": summary}
