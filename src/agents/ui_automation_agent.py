"""Playwright Agent — reasons over the REAL app to write real E2E automation.

(Formerly the "UI Automation" agent — the core is unchanged: an automation-test
generation agent. Renamed to reflect that it targets the full Playwright toolbox.)

It does NOT emit a hardcoded template. It (1) inspects the live DOM of the relevant
page(s) to learn the actual locators, (2) is told the user's requested flow, the
routes, and the credentials, and (3) asks the LLM — guided by a strict ruleset — to
write a complete Playwright spec that performs the journey and asserts the outcome.
Failures from a prior run are fed back so it self-heals.

The ruleset encodes the accessibility-first practices the Playwright team's own test
agents converged on (role/label/text locators, web-first auto-retrying assertions,
assertions derived from the acceptance criteria — not invented). See the design memo
[[playwright-agent]] for the full generate→run→heal roadmap (MCP/a11y grounding,
trace-fed healing, API-based setup) still to be layered on.
"""
import json
import os
import subprocess
from pathlib import Path

from .. import runctx, sandbox
from ..config import PROJECT_ROOT
from ..llm import call_llm_json
from ..state import PipelineState, TestArtifact

SYSTEM = """You are a senior test-automation engineer. Write a COMPLETE Playwright (TypeScript) spec
that AUTOMATES EXACTLY THE REQUESTED FLOW end-to-end against a real running app.

SCOPE — do only what is asked:
- Automate ONLY the flow in TARGET FLOW. Do NOT invent extra journeys (creating records, editing, etc.)
  unless the flow explicitly asks for them. A focused test that PASSES beats a broad one that fails.
- If FUNCTIONAL TEST CASES are provided below, implement EACH ONE as its own test(...) inside a single
  describe — the test title = the case id + title, follow its steps in order, and assert its expected
  result. Cover every case; do not add cases that aren't listed.
- Cases marked automatable=false MUST be written as `test.skip('<id> <title>', async () => {})` with a
  short comment giving the reason (from its note). NEVER try to force a non-automatable case to pass —
  skipping keeps the suite green while recording that the case needs special data / manual verification.

GROUNDING — never guess a locator:
- Every locator MUST correspond to an element that appears in the REAL DOM snapshots below.
- If an element is NOT in the snapshots, DO NOT interact with or assert on it. Never write "assume ...".
- The snapshots include INTERACTIVE STATES marked [STATE: after clicking "..."] — these show the fields
  of a dialog/drawer/form that opens after that click. For any step that opens a form, take the field
  locators from that state's snapshot. To reach it, click the SAME trigger shown in the state label.

LOCATOR HOUSE STYLE — accessibility-first, in this strict priority order (resilience to refactors):
  1. getByRole('<role>', { name: '<accessible name>' })  ← STRONGLY PREFERRED for anything with a label/text
  2. getByLabel(...) for form fields, getByPlaceholder(...), getByText(...)
  3. getByTestId(...) or #id — ONLY when the element has no accessible name/role/text in the snapshot
- NEVER use raw CSS selectors (page.locator('.btn-primary'), nth-child, div > span) or XPath. They are the
  #1 cause of flaky, brittle tests. If you cannot build a role/label/text/testid locator from the snapshot,
  skip that interaction rather than fabricate a CSS/XPath selector.
- Use user-facing accessible names exactly as they appear in the snapshot's role/name/text fields.
- When an ACCESSIBILITY TREE is given for a page/state, it is the AUTHORITATIVE source of roles + accessible
  names (it is exactly what Playwright's getByRole resolves) — build getByRole('<role>', { name }) from it first.

BEHAVIOUR:
- If the flow needs auth, LOG IN FIRST using process.env.LOGIN_EMAIL / process.env.LOGIN_PASSWORD on the
  login page shown below.
- This is a single-page app: after an action, WAIT with web-first AUTO-RETRYING assertions
  (await expect(locator).toBeVisible() / await expect(page).toHaveURL(...)). NEVER use waitForNavigation,
  page.waitForTimeout, or manual sleeps — rely on expect()'s built-in retry to remove timing flake.

ORACLES — assertions come from the requirement, not your imagination:
- Derive each assertion from the FUNCTIONAL TEST CASE's expected result (or the acceptance criteria) — assert
  the exact post-condition it specifies. Do NOT invent, broaden, or weaken assertions to make a test pass.
- Assert a meaningful post-condition proving success (URL changed, the specific expected element/text visible).
  Never assert on HTTP status alone, and never assert only that "the page loads".
- Prefer asserting a specific, user-visible outcome (getByRole/getByText for the expected result) over generic
  visibility of a container.
- Read BASE_URL from process.env.BASE_URL. Group tests in a descriptive test.describe.
- IMPORTS: start the spec with `import { test, expect } from './fixtures';` (NOT '@playwright/test').
  The fixtures capture console/network and the page's accessibility snapshot on failure — do not redefine them.
- If PRIOR RUN FAILURES are given, fix the EXACT locator/assertion that failed — do not rewrite unrelated
  parts. When a failure includes a "PAGE STATE AT FAILURE" accessibility snapshot, build the corrected
  locator from THAT real state (it is the page as it actually was when the step broke); the console/network
  lines tell you whether the real problem is elsewhere (e.g. a 401 login failure, a JS error).
- PRODUCT-MISMATCH TRIAGE: if a prior failure shows the APP simply does not behave as the case expects
  (e.g. it never redirects, an expected element/message genuinely does not exist in the DOM) — i.e. it's
  a product-behaviour finding, NOT a locator/timing bug you can fix from the DOM — then mark that ONE
  case `test.fixme('<id> <title> — needs triage: <one-line reason>', async () => {})`. Keep repairing
  cases whose failure IS a locator/assertion/timing issue. Never loop forever on an unfixable case, and
  never weaken a good assertion just to force a pass.

Respond with ONLY a JSON object:
{"files": [{"path": "generated/e2e/<name>.spec.ts", "covers_ac": [], "tags": ["@e2e"], "content": "<full spec>"}]}"""

_NODE = "/opt/homebrew/opt/node@20/bin/node"


def _explore(base: str, routes: list[str], email: str, password: str, login_path: str = "/login") -> list[dict]:
    """Launch the app, LOG IN (when creds are given), and snapshot the real interactive elements of
    each route the flow touches (the agent's 'eyes'). Returns one snapshot per page. Works for any
    app — authenticated pages are reachable because the explorer signs in first; public apps just
    skip the login step."""
    node = _NODE if Path(_NODE).exists() else "node"
    runner = PROJECT_ROOT / "e2e-runner"
    if not (runner / "explore.cjs").exists():
        return []
    # The explorer needs the app credentials, nothing else the server holds.
    env = sandbox.child_env({"LOGIN_EMAIL": email or "", "LOGIN_PASSWORD": password or "",
                             "LOGIN_PATH": login_path or "/login"})
    try:
        out = subprocess.run([node, "explore.cjs", base, ",".join(routes)], cwd=str(runner),
                             capture_output=True, text=True, timeout=150, env=env)
        line = (out.stdout or "").strip().splitlines()[-1] if out.stdout.strip() else ""
        data = json.loads(line) if line else []
        return data if isinstance(data, list) else [data]
    except Exception as exc:  # exploration is best-effort; the LLM can still infer resilient locators
        return [{"error": str(exc)}]


def _format_dom(snap: dict) -> str:
    head = f"PAGE {snap.get('url')}"
    if snap.get("label"):
        head += f"  [STATE: {snap['label']}]"          # e.g. 'after clicking "Add Customer"'
    head += f"  (title: {snap.get('title', '')})"
    rows = [head]
    for e in snap.get("elements", [])[:28]:  # cap per state to keep the prompt within provider limits
        rows.append("  - " + ", ".join(f"{k}={e[k]}" for k in
                    ("tag", "type", "role", "name", "id", "testid", "placeholder", "ariaLabel", "text") if e.get(k)))
    # A11Y TREE: roles + accessible names exactly as Playwright resolves them — the authoritative getByRole source.
    if snap.get("aria"):
        rows.append("  ACCESSIBILITY TREE (roles + accessible names — build getByRole(name) directly from these):")
        rows.extend("    " + ln for ln in snap["aria"].splitlines()[:60])
    return "\n".join(rows)


def generate_ui_scripts(state: PipelineState) -> dict:
    inp = state.get("story", {}).get("inputs", {}) or {}
    analysis = state.get("repo_analysis") or {}
    attempts = dict(state.get("attempts", {}))
    attempts["generate_ui_scripts"] = attempts.get("generate_ui_scripts", 0) + 1

    base = (inp.get("base_url") or "http://localhost:3000").rstrip("/")
    routes = analysis.get("ui_routes", [])

    # Login is CONFIGURED PER REPO (not app-specific). Fall back to env for the bundled demo only.
    user_email = inp.get("login_user") or os.environ.get("APP_USER") or os.environ.get("IDURAR_USER", "")
    user_pass = inp.get("login_password") or os.environ.get("APP_PASS") or os.environ.get("IDURAR_PASS", "")
    # Login path — the SOURCE OF TRUTH order: explicit config > derived from the repo's code > heuristic.
    login_route = inp.get("login_url") or analysis.get("login_route") or ("/login" if "/login" in routes else "/")
    guard_note = (" The app guards routes and redirects UNAUTHENTICATED users to the login page "
                  "(from the code) — it does NOT redirect already-logged-in users away from it."
                  if analysis.get("auth_guarded") else "")
    auth_note = (f"Login: process.env.LOGIN_EMAIL (default '{user_email}') and process.env.LOGIN_PASSWORD, "
                 f"on {login_route} (derived from the app's routes).{guard_note}" if user_email else
                 "This app appears to need NO login (no credentials configured) — do not add a login step "
                 "unless the DOM clearly shows a login form.")

    cases = state.get("functional_cases") or []
    if cases:
        goal = (f"Automate the {len(cases)} FUNCTIONAL TEST CASE(S) listed below — one Playwright test per "
                f"case, each following its steps and asserting its expected result. If a case needs auth, "
                f"log in first.")
    else:
        goal = (inp.get("scope_prompt") or "").strip() or (
            "Log in with the provided credentials and verify the app's main screen loads "
            "(the URL leaves the login page and a known post-login element is visible)." if user_email else
            "Exercise the app's primary landing screen — locate its main interactive elements and assert the "
            "key content is visible.")

    # --- Launch the app, (log in,) and analyze the real DOM of the pages THIS flow touches ---
    explore = [login_route, "/"]                         # login form (if any) + main screen
    gl = goal.lower()
    for r in routes:                                     # add routes explicitly mentioned in the flow
        seg = r.strip("/").split("/")[0].lower()
        if seg and seg in gl and r not in explore:
            explore.append(r)
    explore = explore[:4]                                # cap for prompt size + exploration time
    # Mock runs use the canned spec — don't launch a browser / require the app to be up.
    snaps = [] if runctx.is_mock() else \
        [s for s in _explore(base, explore, user_email, user_pass, login_route) if s.get("elements")]
    dom = "\n\n".join(_format_dom(s) for s in snaps) or "(live DOM unavailable — infer resilient locators.)"
    context = [
        f"TARGET FLOW (do exactly this): {goal}",
    ]
    if cases:
        context.append("FUNCTIONAL TEST CASES to automate (one test per case; test.skip the ones with "
                       "automatable=false, citing the note):\n"
                       + "\n".join(f"- {c.get('id')}: {c.get('title')} | automatable: {c.get('automatable', True)}"
                                   + (f" | note: {c.get('automation_note')}" if not c.get('automatable', True) else "")
                                   + f" | steps: {c.get('steps')} | expected: {c.get('expected_result')}"
                                   for c in cases[:14]))
    context += [
        f"Base URL (process.env.BASE_URL): {base}",
        f"App stack: {analysis.get('stack', 'web app')}",
        f"Known UI routes: {', '.join(routes[:25]) if routes else 'unknown'}",
        auth_note,
        "REAL DOM of the relevant page(s) — build locators from these actual elements:",
        dom,
    ]
    # If the destination repo already has a test framework, scan it and REUSE it
    # (page objects, fixtures, structure) instead of reinventing — "build on top".
    dest = inp.get("dest_repo")
    if dest:
        try:
            from ..integration import github
            if github.connected():
                fw = github.scan_framework(dest)
                if fw.get("ok") and fw.get("has_framework"):
                    context.append("EXISTING FRAMEWORK in the destination repo — REUSE it, do not reinvent:")
                    context.append(f"  framework(s): {', '.join(fw['frameworks'])}; config: {fw.get('configs')}")
                    if fw.get("page_objects"):
                        context.append(f"  reuse these page objects: {fw['page_objects'][:15]}")
                    if fw.get("helpers"):
                        context.append(f"  reuse these fixtures/helpers: {fw['helpers'][:15]}")
                    context.append("  Import & extend the existing page objects/fixtures; match the existing "
                                   "folder structure and naming conventions.")
        except Exception:
            pass

    for r in reversed(state.get("run_results", [])):
        if r["suite"] in ("feature", "jenkins") and r.get("failed", 0) > 0:
            src = "Jenkins CI" if r["suite"] == "jenkins" else "local run"
            context.append(f"\nPRIOR {src} FAILED — fix exactly these locator/assertion errors:\n{r.get('failures')}")
            break

    # ORACLE FAILURES: a case PASSED but its assertion does not truly verify the requirement (a false pass).
    # Restore a faithful assertion of the expected result, or test.fixme it — never weaken it to force green.
    for f in state.get("oracle_findings", []) or []:
        context.append(f"\nORACLE FAILURE — case {f.get('case_id')} currently passes WITHOUT verifying its "
                       f"expected result: {f.get('issue')}. Restore an assertion that faithfully checks that "
                       f"case's expected result. If the app genuinely cannot satisfy it, mark that ONE case "
                       f"test.fixme('<id> <title> — needs triage: <reason>'). Do NOT weaken the assertion.")

    # Generate — and if the model returns NO files (empty/truncated, common on small free models
    # for large prompts), retry with a firmer instruction. Never silently succeed with 0 specs.
    files = []
    prompt = "\n".join(context)
    for tryn in range(3):
        try:
            raw = call_llm_json("ui_automation_agent", SYSTEM, prompt, max_tokens=6000)
            files = [f for f in (raw.get("files") or []) if f.get("content", "").strip()]
        except Exception as exc:
            print(f"  [Playwright Agent] generation attempt {tryn + 1} errored: {exc}")
            files = []
        if files:
            break
        prompt = ("\n".join(context) +
                  "\n\nIMPORTANT: You returned no spec. You MUST return at least one complete, runnable "
                  "spec file in the exact JSON shape, with real code in 'content'. Focus on the single "
                  "most important scenario of the TARGET FLOW and ground every locator in the DOM above.")
    if not files:
        raise RuntimeError("Playwright agent produced no spec — the LLM returned empty output after retries "
                           "(model/token limit). Try a stronger model or a narrower scope prompt.")

    artifacts = list(state.get("test_artifacts", []))
    for f in files:
        out = sandbox.run_workspace() / Path(f["path"]).relative_to("generated")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(f["content"])
        artifacts.append(TestArtifact(type="playwright", path=str(out),
                                      covers_ac=f.get("covers_ac", []), tags=f.get("tags", [])).model_dump())

    print(f"  [Playwright Agent] attempt {attempts['generate_ui_scripts']}: generated {len(files)} "
          f"spec(s) grounded in real DOM ({sum(len(s.get('elements', [])) for s in snaps)} elements seen)")
    return {"test_artifacts": artifacts, "attempts": attempts}
