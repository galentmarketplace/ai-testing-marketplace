"""Delivery — a two-phase, PR-last flow.

  push_branch : create/scaffold the destination repo (if new), then commit the generated
                code onto a working BRANCH — no PR yet. This is what Jenkins checks out and
                what Self-Heal pushes fixes onto.
  open_pr     : run at the very END, only after the Jenkins gate is green — opens the actual
                pull request from that branch, so reviewers only ever see a green, ready-to-
                merge PR.

Both fall back to a draft when GitHub isn't connected or no destination is configured.
"""
import uuid
from pathlib import Path

from .. import runctx
from ..integration import github, scaffold
from ..integration import go_coverage as gocov
from ..llm import call_llm_json
from ..state import PipelineState

SYSTEM = """You write pull request descriptions. Given the story/target, the generated tests,
and the quality-gate results, draft a concise PR title and body: what the suite covers,
which flows/endpoints, and the gate outcomes.
The file list you are given is EXHAUSTIVE and is the diff. Describe those files and nothing
else. Never state that no files were added, that no tests were needed, or that an existing
suite already satisfies a threshold — you cannot see the repository, only this diff.
Respond with ONLY a JSON object: {"title": "...", "body": "..."}"""

# generated file suffix -> folder it lands in inside the destination repo
_DEST = {".spec.ts": "tests", ".spec.js": "tests", ".k6.js": "perf",
         ".axe.json": "a11y", ".pact.json": "contract", ".semgrep.yml": "security"}

# Run output, not source. JUnit reports were landing in tests/ beside the specs.
_NEVER_COMMIT = {".xml", ".log", ".zip", ".png", ".webm"}


def _all_files(state: PipelineState) -> list[str]:
    """Every generated file this PR carries, across tracks — the drafter's only view of the diff."""
    names = [Path(t["path"]).name for t in state.get("test_artifacts", [])
             if Path(t["path"]).suffix not in _NEVER_COMMIT]
    names += [a["repo_path"] for a in state.get("coverage_artifacts", []) if a.get("repo_path")]
    names += [Path(a["path"]).name for a in (state.get("security_artifacts", [])
                                             + state.get("a11y_artifacts", [])
                                             + state.get("contract_artifacts", []))]
    return names


def _coverage_facts(state: PipelineState) -> dict | None:
    """What a coverage run actually did, from the measurements — not from the model.

    A model asked to describe a PR it cannot see will describe a plausible one. Live, it
    wrote "No new test files were added as part of this PR" and "the existing test suite
    already satisfies the required coverage threshold" onto a PR that added two test files
    and raised coverage from 28.6% against an 80% threshold. Both statements were the exact
    opposite of the diff. So for a coverage fix the narrative is built from the numbers.
    """
    rep = state.get("coverage_report") or {}
    prop = rep.get("proposed_tests") or {}
    tests = [a for a in state.get("coverage_artifacts", [])
             if a.get("type") == "coverage-tests" and a.get("repo_path")]
    if not rep.get("ok") or not prop.get("proposed") or not tests:
        return None
    after = rep.get("after") or {}
    # Prefer measured over claimed here too — this body is read by a reviewer deciding whether
    # to merge, and "covering 0 function(s)" on a PR that covered four is a false report.
    covers = [f["func"] for f in gocov.functions_fixed(rep)] or (prop.get("covers") or [])
    return {"before": rep.get("total_pct"),
            "after": rep.get("total_pct_after", after.get("total_pct")),
            "min_pct": rep.get("min_pct"),
            "files": [a["repo_path"] for a in tests],
            "covers": covers,
            "skipped": prop.get("skipped") or [],
            "verified": bool(prop.get("verified")),
            "note": prop.get("note", ""),
            "remaining": len((after.get("uncovered_funcs") if after.get("ok")
                              else rep.get("uncovered_funcs")) or [])}


def _coverage_draft(f: dict) -> dict:
    before, after, floor = f["before"], f["after"], f["min_pct"]
    n, funcs = len(f["files"]), f["covers"]
    move = (f"{before:.1f}% → {after:.1f}%" if after is not None else f"{before:.1f}%")
    title = (f"Raise test coverage {move}" if after is not None
             else f"Add unit tests for {len(funcs)} uncovered function(s)")

    body = ["## Summary", "",
            f"Adds **{n} generated unit test file(s)** covering "
            f"{len(funcs)} function(s) that had **0% coverage**.", ""]
    if after is not None:
        body += ["| | Before | After |", "|---|---|---|",
                 f"| Statement coverage | {before:.1f}% | **{after:.1f}%** |"]
        if floor:
            body.append(f"| Threshold ({floor:.0f}%) | "
                        + ("met" if before >= floor else "**not met**") + " | "
                        + ("**met**" if after >= floor else "**still not met**") + " |")
        body.append("")
    if funcs:
        body += ["### Functions now covered", ""] + [f"- `{c}`" for c in funcs] + [""]
    body += ["### Files added", ""] + [f"- `{p}`" for p in f["files"]] + [""]
    body += ["### Verification", "",
             ("✅ `go test ./...` compiled and passed with these tests in place, and coverage "
              "was re-measured afterwards — the figure above is measured, not projected."
              if f["verified"] else
              f"⚠️ Not execution-verified: {f['note']}")]
    if f["skipped"]:
        body += ["", "### Deliberately not tested", ""]
        body += [f"- `{s.get('func')}` — {s.get('reason')}" for s in f["skipped"][:10]]
    if f["remaining"]:
        body += ["", f"{f['remaining']} function(s) remain uncovered — see `coverage-report.md`."]
    return {"title": title, "body": "\n".join(body)}


def _draft(state: PipelineState) -> dict:
    facts = _coverage_facts(state)
    if facts:
        return _coverage_draft(facts)
    try:
        user = (f"Target: {state.get('story', {}).get('inputs', {})}\n"
                f"AC: {state.get('acceptance_criteria', '(none)')}\n"
                f"Gates: {[(g['gate'], g['verdict']) for g in state.get('gate_decisions', [])]}\n"
                f"Files in this PR (exhaustive): {_all_files(state)}")
        raw = call_llm_json("pr_agent", SYSTEM, user)
        return {"title": raw["title"], "body": raw["body"]}
    except Exception:
        files = [Path(t["path"]).name for t in state.get("test_artifacts", [])]
        return {"title": "AI-generated test suite",
                "body": "Automated tests generated by the AI Testing Marketplace.\n\n"
                        + "\n".join(f"- {f}" for f in files)}


def _collect_files(state: PipelineState) -> tuple[dict, bool]:
    """The generated artifacts to publish, keyed by their destination path."""
    files: dict[str, str] = {}
    for art in state.get("test_artifacts", []):
        p = Path(art["path"])
        if p.suffix in _NEVER_COMMIT:
            continue            # run reports are evidence, not source — they belong to the run
        folder = next((v for suf, v in _DEST.items() if p.name.endswith(suf)), "tests")
        files[f"{folder}/{p.name}"] = p.read_text()
        # Every generated spec starts `import { test, expect } from './fixtures'`. Those
        # fixtures live beside the spec in the run workspace, so the LOCAL run passes — but
        # they were never committed, so CI checked out a spec importing a module that does
        # not exist and the whole suite failed to load before a single test ran. Ship them.
        if p.name.endswith((".spec.ts", ".spec.js")):
            fx = p.parent / "fixtures.ts"
            if fx.is_file():
                files[f"{folder}/fixtures.ts"] = fx.read_text()
    # Generated Go tests: verified by compiling and running them in the clone, then never
    # committed, so the coverage fix never reached a pull request. A Go test must keep its
    # repo-relative path — beside the package it tests — not be flattened into tests/.
    for art in state.get("coverage_artifacts", []):
        rel = art.get("repo_path")
        p = Path(art["path"])
        if not rel or not p.is_file() or not rel.endswith("_test.go"):
            continue
        files[rel] = p.read_text()
    for art in (state.get("security_artifacts", []) + state.get("a11y_artifacts", [])
                + state.get("contract_artifacts", [])):
        p = Path(art["path"])
        folder = next((v for suf, v in _DEST.items() if p.name.endswith(suf)), "scans")
        files[f"{folder}/{p.name}"] = p.read_text()
    has_automation = (any(a.get("type") == "playwright" for a in state.get("test_artifacts", []))
                      or any(a.get("type") == "coverage-tests" for a in state.get("coverage_artifacts", [])))
    return files, has_automation


def push_branch(state: PipelineState) -> dict:
    """Phase 1: commit the generated code onto a working branch (no PR yet)."""
    inp = state.get("story", {}).get("inputs", {}) or {}
    dest = inp.get("dest_repo")
    new_repo = inp.get("new_repo")

    # Mock runs are fully simulated — never touch GitHub (no repo, branch, or PR side-effects).
    if runctx.is_mock():
        print("  [Delivery] mock run — skipping GitHub (no branch pushed)")
        return {"pr": {"status": "mock", "has_automation": True,
                       "url": "(mock run — no branch pushed, no PR opened)"}}

    # Regression targets an EXISTING suite repo (source_repo) rather than dest/new — recognise it here.
    tracks = set(state.get("run_config", {}).get("tracks", []))
    src_repo = inp.get("source_repo") or ""
    is_full_src = bool(src_repo) and src_repo.count("/") == 1 and not src_repo.startswith(("http", "repos/", "/", "."))
    reg_target = src_repo if ("regression" in tracks and is_full_src) else None

    if not github.connected() or not (dest or new_repo or reg_target):
        print("  [Delivery] no GitHub target — will draft a PR at the end")
        return {"pr": {"status": "draft", "url": "(draft only — connect GitHub + pick a destination repo to publish)"}}

    # Nothing generated to publish? Do NOT create a repo / scaffold / open an empty PR.
    gen, has_automation = _collect_files(state)
    if not gen:
        # Regression runs the repo's EXISTING cases — no generated files, but we still open a
        # working branch off the suite repo so Self-Heal can commit repairs and we can PR them.
        src = reg_target or (dest if (dest and "/" in dest) else "")
        if src:
            base = github.repo_info(src).get("default_branch", "main")
            branch = f"ai-reg/{uuid.uuid4().hex[:8]}"
            github.ensure_branch(src, branch, base)
            print(f"  [Delivery] regression working branch {src}@{branch} (Self-Heal commits repairs here)")
            return {"pr": {"repo": src, "branch": branch, "base": base, "status": "branch",
                           "has_automation": False, "files": 0}}
        print("  [Delivery] no generated tests — nothing to publish (skipping branch/PR)")
        return {"pr": {"status": "empty", "has_automation": False,
                       "url": "(no tests were generated — nothing to publish; check the UI agent output)"}}

    files, scaffolded = {}, False
    if new_repo:
        created = github.create_repo(new_repo, private=True,
                                     description="AI-generated test suite (AI Testing Marketplace)")
        dest = created["full_name"]

    # Ensure the destination has a runnable Playwright framework (package.json, config, pages,
    # fixtures) — scaffold it when missing so Jenkins' `npm install` / `npx playwright test` works.
    # If the repo already has a framework, we reuse it (the UI agent scans + builds on top).
    # Only scaffold a Playwright framework when this run actually produced Playwright specs.
    # Keying off "the destination has no framework" scaffolded package.json, playwright.config.ts
    # and page objects into a GO repository whose pull request contained one generated Go test.
    wants_playwright = any(a.get("type") == "playwright" for a in state.get("test_artifacts", []))
    need_framework = False
    if wants_playwright:
        need_framework = True
        if not new_repo:
            try:
                fw = github.scan_framework(dest)
                need_framework = not (fw.get("ok") and fw.get("has_framework"))
            except Exception:
                need_framework = True
    if need_framework:
        files.update(scaffold.playwright_framework(
            base_url=inp.get("base_url", ""), app_name=inp.get("source_repo", dest)))
        scaffolded = True
        print(f"  [Delivery] {'created ' + dest + ' + ' if new_repo else ''}scaffolded Playwright framework "
              f"({'new repo' if new_repo else 'existing repo had none'})")

    files.update(gen)

    import time
    branch = f"ai-tests/{uuid.uuid4().hex[:8]}"
    r, last = None, None
    for attempt in range(3):   # GitHub API can transiently time out — retry (idempotent on the same branch)
        try:
            r = github.commit_files(dest, files, branch=branch, message="ci: publish generated tests")
            break
        except Exception as exc:
            last = exc
            print(f"  [Delivery] push attempt {attempt + 1} failed ({exc}); retrying…")
            time.sleep(2 * (attempt + 1))
    if r is None:
        raise last
    print(f"  [Delivery] pushed {r['committed']} file(s) to {dest}@{branch} (PR opens after CI is green)")
    return {"pr": {"repo": dest, "branch": branch, "base": r["base"], "files": r["committed"],
                   "scaffolded": scaffolded, "has_automation": has_automation, "status": "branch"}}


def open_pr(state: PipelineState) -> dict:
    """Phase 2 (final): open the PR from the branch — reached only once every gate, including
    the Jenkins gate, is green. So the PR is ready to merge the moment it appears."""
    pr = state.get("pr", {}) or {}
    draft = _draft(state)

    # Mock runs never open a real PR — show a drafted title/body only.
    if runctx.is_mock():
        print("  [PR Agent] mock run — PR drafted, not opened")
        return {"pr": {**pr, **draft, "url": "(mock run — PR not opened)", "mock": True}, "status": "done"}

    if not github.connected() or not pr.get("repo") or not pr.get("branch"):
        print(f"  [PR Agent] drafted PR (no GitHub target): {draft['title']}")
        return {"pr": {**pr, **draft, "url": pr.get("url", "(draft only)")}, "status": "done"}

    # No changes on the branch (e.g. regression passed first try, or nothing healed) → no PR to open.
    if github.compare_ahead(pr["repo"], pr.get("base", "main"), pr["branch"]) == 0:
        print("  [PR Agent] branch has no changes — no PR to open (nothing to merge)")
        return {"pr": {**pr, **draft, "url": "(no changes — no PR needed)"}, "status": "done"}

    jr = state.get("jenkins_report", {}) or {}
    ci_line = ""
    if jr.get("status") == "ran":
        ci_line = (f"\n\n> **CI:** Jenkins {jr.get('overall')} — "
                   f"{jr.get('passed', 0)} passed, {jr.get('failed', 0)} failed.")
    scaffold_line = ("\n\n> Includes a scaffolded Playwright framework (pages/, fixtures/, config, CI)."
                     if pr.get("scaffolded") else "")
    body = draft["body"] + scaffold_line + ci_line + "\n\n✅ Automation is green — ready to merge."

    result = github.open_pr(pr["repo"], pr["branch"], pr.get("base", "main"), draft["title"], body)
    print(f"  [PR Agent] opened PR #{result['number']} on {pr['repo']}: {result['url']} (ready to merge)")
    return {"pr": {**pr, **draft, "url": result["url"], "number": result["number"]}, "status": "done"}
