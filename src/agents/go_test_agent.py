"""Go Test Generation agent — phase 2 of coverage (the EarlyAI pattern): propose unit tests for the
functions the coverage run found at 0%, grounded in their ACTUAL source.

Execution-in-the-loop: when `go` is available the proposals are dropped into the cloned repo, compiled
and run (`go test ./...`); anything that doesn't compile/pass is withdrawn, and coverage is re-measured
so the gate sees the REAL post-generation number. Without `go` the proposals are still written as
artifacts but marked unverified — never presented as green. Generated files never touch the user's
working tree: they go to generated/coverage/tests/ (and only into OUR clone for verification).
"""
import json
import os
import shutil
import subprocess
from pathlib import Path

from .. import runctx, sandbox
from ..integration import go_coverage as gocov
from ..integration import reports
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


def _resolve(root: Path, rel: str) -> Path | None:
    """Find the file on disk for a path `go tool cover` reported.

    Coverage paths are MODULE-QUALIFIED — "github.com/org/repo/internal/pricing/pricing.go" —
    while the clone holds "internal/pricing/pricing.go". Joining them blindly produced a path
    that does not exist, so every function came back "source unavailable" and the agent
    skipped all of them: the coverage fix never got written.
    """
    rel = (rel or "").strip()
    if not rel:
        return None
    direct = root / rel
    if direct.is_file():
        return direct
    parts = Path(rel).parts
    # drop leading module segments until the remainder exists under the clone
    for i in range(1, len(parts)):
        cand = root.joinpath(*parts[i:])
        if cand.is_file():
            return cand
    # last resort: a unique basename match
    hits = [p for p in root.rglob(Path(rel).name) if p.is_file()]
    return hits[0] if len(hits) == 1 else None


def _func_source(root: Path, rel: str, line: int | None, max_lines: int = 90) -> str:
    """Package clause + imports + the function body (brace-balanced) so the LLM tests the real signature."""
    path = _resolve(root, rel)
    if path is None:
        return ""
    try:
        lines = path.read_text(errors="ignore").splitlines()
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
    # Type declarations from the same file. Without them a test for
    # `Count(items []Item) int` has to guess Item's fields, and guessed fields do not compile.
    types, tdepth, in_type = [], 0, False
    for ln in lines:
        if not in_type and ln.startswith("type "):
            in_type, tdepth = True, 0
        if in_type:
            types.append(ln)
            tdepth += ln.count("{") - ln.count("}")
            if tdepth <= 0 and (ln.rstrip().endswith("}") or "{" not in ln):
                in_type = False
                types.append("")
        if len(types) > 60:
            break

    body, depth, started = [], 0, False
    for ln in lines[max(0, (line or 1) - 1):(line or 1) - 1 + max_lines]:
        body.append(ln)
        depth += ln.count("{") - ln.count("}")
        started = started or "{" in ln
        if started and depth <= 0:
            break
    return "\n".join(head + imp + ["", *types, *body])


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
        raw = call_llm_json("go_test_agent", SYSTEM, "\n".join(ctx),
                            max_tokens=int(os.environ.get("ATM_GOTEST_MAX_TOKENS", "16000")))
    except Exception as exc:
        # Returning {} left no trace: the coverage report still read as a plain gap report,
        # so a failed generation and a run that never attempted one looked identical.
        print(f"  [Go Test Gen] generation FAILED for {len(targets)} uncovered function(s): {exc}")
        rep["proposed_tests"] = {"proposed": 0, "covers": [], "skipped": [], "verified": False,
                                 "coverage_after_pct": None,
                                 "note": f"the generator errored: {exc}"}
        return {"coverage_report": rep, "go_test_proposals": rep["proposed_tests"]}
    returned_raw = raw.get("files") or []
    files = [f for f in returned_raw if f.get("content") and f.get("path", "").endswith("_test.go")]
    skipped = raw.get("skipped") or []
    if not files:
        # Seen live: the generator answered, nothing usable came out, and the run carried on
        # silently — the report showed the gap as if no attempt had been made. Say what came
        # back, so a no-op is diagnosable instead of looking like a run that chose not to try.
        bad = [f"{f.get('path') or '(no path)'}"
               f"{'' if f.get('content') else ' [empty]'}" for f in returned_raw]
        why = (f"{len(returned_raw)} entry/entries, none usable: {bad}" if returned_raw
               else "the generator returned no files at all")
        print(f"  [Go Test Gen] NO tests proposed for {len(targets)} uncovered function(s) — {why}")
        rep["proposed_tests"] = {"proposed": 0, "covers": [], "skipped": skipped,
                                 "verified": False, "coverage_after_pct": None,
                                 "note": f"no usable test file was generated ({why})"}
        return {"coverage_report": rep, "go_test_proposals": rep["proposed_tests"]}
    # A skip whose reason points at a file that was never returned is not a skip, it is a
    # dropped function. Seen live: BulkDiscount and LoyaltyPoints were "covered in
    # pricing_generated_test.go", which the model did not produce, so they silently went
    # untested while the run reported success.
    returned = {f.get("path", "") for f in files}
    phantom = [s_ for s_ in skipped
               if "_test.go" in str(s_.get("reason", ""))
               and not any(str(s_.get("reason", "")).find(Path(r).name) >= 0 for r in returned)]
    if phantom:
        names = [s_.get("func") for s_ in phantom]
        print(f"  [Go Test Gen] {len(phantom)} function(s) were skipped citing a file that was "
              f"never returned — reporting them as UNTESTED, not skipped: {names}")
        for s_ in phantom:
            s_["reason"] = (f"NOT TESTED — the generator claimed a file it did not return "
                            f"({s_.get('reason', '')[:80]})")

    out_dir = sandbox.run_workspace("coverage/tests")
    arts = list(state.get("coverage_artifacts", []))
    for f in files:
        dst = out_dir / f["path"]
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(f["content"])
        # repo_path is what delivery needs: a Go test MUST sit beside the package it tests,
        # so it cannot be flattened into a tests/ folder like a Playwright spec.
        arts.append({"type": "coverage-tests", "path": str(dst), "repo_path": f["path"],
                     "tags": ["@coverage", "@generated-test"], "covers": f.get("covers", [])})

    # EXECUTION-IN-THE-LOOP: verify in OUR clone, withdraw anything red, re-measure.
    verified, after, note = False, None, ""
    if not mock and files and shutil.which("go") and root and root.exists():
        placed = []
        for f in files:
            dst = root / f["path"]
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_text(f["content"])
            placed.append(dst)
        t = subprocess.run(["go", "test", "./..."], cwd=str(root), capture_output=True, text=True, timeout=600,
                           env=sandbox.child_env())
        if t.returncode == 0:
            verified = True
            rep2 = gocov.run_go_coverage(str(root))
            after = rep2.get("total_pct") if rep2.get("ok") else None
            # Keep the WHOLE post-fix report, not just the headline number: the artifact has to
            # show which functions moved and what is still uncovered. Reporting only the before
            # state made a run that raised coverage read exactly like one that failed to.
            if rep2.get("ok"):
                rep["after"] = {k: v for k, v in rep2.items() if k != "lines"}
                rep["after_lines"] = rep2.get("lines")
            note = "compiled & passed in the cloned repo; coverage re-measured"
        else:
            note = "withdrawn — generated tests did not compile/pass: " + (t.stderr or t.stdout or "")[-400:].strip()
        # Clean up whether they passed or failed. The clone is shared between runs, and a
        # verified test left behind makes the NEXT run measure a tree that matches no branch:
        # it sees the gap already closed, generates nothing, and opens no pull request. The
        # file that matters is the copy in the run workspace, which is what delivery commits.
        for pl in placed:
            pl.unlink(missing_ok=True)
    elif not mock and files:
        note = "unverified — `go` toolchain not installed on this host (brew install go)"
    elif mock:
        note = "mock run — proposals not executed"

    # `covers` is ground truth where we have it: the functions whose measured coverage moved
    # off 0%. The model's own claim is only a fallback for an unverified run.
    measured = [f["func"] for f in gocov.functions_fixed(rep)]
    claimed = sorted({c for f in files for c in f.get("covers", [])})
    summary = {"proposed": len(files), "covers": measured or claimed, "covers_measured": bool(measured),
               "skipped": skipped, "verified": verified, "coverage_after_pct": after, "note": note}
    rep["proposed_tests"] = summary
    if verified and after is not None:
        rep["total_pct_after"] = after
    arts = _rewrite_artifacts(state, rep, arts)
    print(f"  [Go Test Gen] proposed {len(files)} test file(s) for {len(summary['covers'])} function(s), "
          f"skipped {len(skipped)} · {note}" + (f" · coverage {rep.get('total_pct')}% → {after}%" if after is not None else ""))
    return {"coverage_artifacts": arts, "coverage_report": rep, "go_test_proposals": summary}


def _rewrite_artifacts(state: PipelineState, rep: dict, arts: list[dict]) -> list[dict]:
    """Re-emit the coverage artifacts now that the gap has been closed.

    The coverage step writes its report BEFORE this agent runs, so the artifact a reviewer
    opens at the end of the run showed the gap and not the fix — and the LCOV/Cobertura
    exports described a tree that no longer matches the branch being delivered.
    """
    after = rep.get("after") or {}
    if not after.get("ok"):
        return arts
    inp = state.get("story", {}).get("inputs", {}) or {}
    repo = inp.get("source_repo") or inp.get("repo") or "repo"
    min_pct = rep.get("min_pct") or 80.0
    by_type = {a.get("type"): a.get("path") for a in arts}
    cov = {"lines": rep.get("after_lines") or {}, "total_pct": after["total_pct"]}
    written = []
    try:
        if by_type.get("coverage-report"):
            Path(by_type["coverage-report"]).write_text(gocov.report_markdown(rep, repo, min_pct))
            written.append("coverage-report.md")
        if by_type.get("coverage-json"):
            Path(by_type["coverage-json"]).write_text(json.dumps(
                {k: v for k, v in rep.items() if k != "after_lines"}, indent=2, default=str))
            written.append("coverage.json")
        if cov["lines"] and by_type.get("coverage-lcov"):
            Path(by_type["coverage-lcov"]).write_text(reports.lcov(cov))
            written.append("coverage.lcov")
        if cov["lines"] and by_type.get("coverage-cobertura"):
            Path(by_type["coverage-cobertura"]).write_text(reports.cobertura_xml(cov))
            written.append("cobertura.xml")
    except Exception as exc:                       # an artifact rewrite must never fail a run
        print(f"  [Go Test Gen] could not refresh coverage artifacts ({exc})")
        return arts
    if written:
        print(f"  [Go Test Gen] refreshed {', '.join(written)} with the post-fix numbers "
              f"({rep['total_pct']:.1f}% → {after['total_pct']:.1f}%)")
    return arts
