"""Functional Test Case agent — derives explicit FUNCTIONAL TEST CASES from the acceptance
criteria (the source of truth: a Jira ticket / pasted AC), BEFORE any automation is written.

This is the production-grade separation of concerns:
  Requirements (Jira)  →  Acceptance Criteria  →  FUNCTIONAL TEST CASES  →  automation (case-wise)

Each case is a concrete, traceable scenario (id, title, preconditions, ordered steps, expected
result, the AC ids it covers). The UI Automation agent then automates each case one-by-one,
grounded in the real DOM — so nothing is invented; every spec traces back to a requirement.
"""
import json

from ..config import GENERATED_DIR
from ..llm import call_llm_json
from ..mocks import MOCK_RESPONSES
from ..state import PipelineState

MOCK_RESPONSES.setdefault("functional_case_agent", json.dumps({"cases": [
    {"id": "FC-1", "title": "Successful login shows the dashboard", "priority": "must",
     "preconditions": ["A valid user account exists"],
     "steps": ["Go to the login page", "Enter valid email and password", "Click Log In"],
     "expected_result": "The dashboard loads and the user's home screen is visible",
     "covers_ac": ["AC-1"], "tags": ["login", "smoke"]},
    {"id": "FC-2", "title": "Invalid credentials are rejected", "priority": "must",
     "preconditions": ["On the login page"],
     "steps": ["Enter an invalid email/password", "Click Log In"],
     "expected_result": "An error message is shown and the dashboard does not load",
     "covers_ac": ["AC-1"], "tags": ["login", "negative"]},
]}))

SYSTEM = """You are a senior QA analyst. Given ACCEPTANCE CRITERIA (the source of truth) and the app
context, produce a COMPLETE, TRACEABLE set of FUNCTIONAL TEST CASES.

Rules:
- Derive cases ONLY from the acceptance criteria + app context. Do NOT invent features not implied.
- Cover: the happy path, negative/validation cases, and meaningful edge cases.
- Each case must be concrete and independently executable, with ordered UI steps.
- Trace every case back to the AC id(s) it verifies (covers_ac).
- For each case set "automatable": true only if it can be automated end-to-end against the LIVE app
  with the ONE provided test account and no external setup. Set it false when the case needs special
  test data (e.g. a separately-provisioned locked/expired account), an external condition you cannot
  induce (service outage, network failure, rate-limit lockout), or manual/visual verification — and
  put the reason in "automation_note". Be honest: a case that can't be driven with the given account
  is NOT automatable.

Respond with ONLY a JSON object:
{"cases": [{"id": "FC-1", "title": "...", "priority": "must|should|could",
  "preconditions": ["..."], "steps": ["...","..."], "expected_result": "...",
  "covers_ac": ["AC-1"], "tags": ["..."], "automatable": true, "automation_note": ""}]}"""


def _md(cases: list[dict], repo: str) -> str:
    lines = [f"# Functional Test Cases — {repo}", "", f"{len(cases)} case(s), derived from the acceptance criteria.", ""]
    for c in cases:
        lines += [f"## {c.get('id')} · {c.get('title')}  _(priority: {c.get('priority', '—')})_",
                  f"- **Covers:** {', '.join(c.get('covers_ac', []) or []) or '—'}",
                  f"- **Preconditions:** {'; '.join(c.get('preconditions', []) or []) or '—'}",
                  "- **Steps:**"]
        lines += [f"  {i + 1}. {s}" for i, s in enumerate(c.get("steps", []) or [])]
        lines += [f"- **Expected:** {c.get('expected_result', '—')}", ""]
    return "\n".join(lines)


def generate_functional_cases(state: PipelineState) -> dict:
    inp = state.get("story", {}).get("inputs", {}) or {}
    ac = state.get("acceptance_criteria")
    analysis = state.get("repo_analysis") or {}
    scope = (inp.get("scope_prompt") or "").strip()

    ctx = [f"Acceptance criteria (source of truth):\n{json.dumps(ac) if ac else '(none provided — infer from the scope + app)'}"]
    if scope:
        ctx.append(f"Scope / focus: {scope}")
    if analysis.get("ui_routes"):
        ctx.append(f"App UI routes (from the code — the source of truth): {', '.join(analysis['ui_routes'][:25])}")
    if analysis.get("login_route"):
        ctx.append(f"Login route (from the code): {analysis['login_route']}")
    if analysis.get("auth_guarded"):
        ctx.append("Auth behaviour (from the code): the app redirects UNAUTHENTICATED users to the login "
                   "page. It does NOT redirect already-logged-in users away from the login page — do not "
                   "write cases that assume behaviours the code does not implement.")
    ctx.append(f"App stack: {analysis.get('stack', 'web app')}")

    raw = call_llm_json("functional_case_agent", SYSTEM, "\n\n".join(ctx), max_tokens=5000)
    cases = [c for c in (raw.get("cases") or []) if c.get("title")]
    if not cases:
        raise RuntimeError("Functional Case agent produced no cases — the LLM returned empty "
                           "(model/limit). Provide clearer acceptance criteria or use a stronger model.")

    repo = inp.get("source_repo") or inp.get("repo") or "app"
    out = GENERATED_DIR / "functional" / "functional-test-cases.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(_md(cases, repo))
    arts = list(state.get("functional_artifacts", []))
    arts.append({"type": "functional-cases", "path": str(out), "tags": ["@functional"]})

    # INTENT DSL (YAML): a portable, human-readable, diffable natural-language spec of each case — the same
    # shape agent-native platforms use — storable in Jira/git and consumable by the Playwright agent.
    try:
        import yaml
        story = state.get("story", {}) or {}
        jira_t = ((state.get("run_config", {}) or {}).get("jira") or {}).get("ticket")
        intent = {
            "suite": {"id": story.get("id"), "title": story.get("title"),
                      "source": f"jira:{jira_t}" if jira_t else "acceptance-criteria", "app": repo},
            "cases": [{
                "id": c.get("id"), "title": c.get("title"), "priority": c.get("priority"),
                "intent": c.get("title"),
                "preconditions": c.get("preconditions") or [],
                "steps": c.get("steps") or [],
                "expect": c.get("expected_result"),
                "covers": c.get("covers_ac") or [],
                "automatable": bool(c.get("automatable", True)),
                **({"note": c.get("automation_note")} if c.get("automation_note") else {}),
            } for c in cases],
        }
        yout = GENERATED_DIR / "functional" / "functional-cases.yaml"
        yout.write_text(yaml.safe_dump(intent, sort_keys=False, allow_unicode=True, width=100))
        arts.append({"type": "functional-cases-yaml", "path": str(yout), "tags": ["@functional", "@intent"]})
    except Exception as exc:  # the YAML is a convenience artifact — never fail the run over it
        print(f"  [Functional Cases] intent YAML skipped: {exc}")

    # Store the cases BACK in Jira (system of record) — sub-tasks under the ticket, else a comment.
    jira_cfg = (state.get("run_config", {}) or {}).get("jira")
    jira_result = None
    if jira_cfg and jira_cfg.get("ticket"):
        try:
            from ..integration import jira
            jira_result = jira.create_test_cases(jira_cfg["url"], jira_cfg["email"], jira_cfg["token"],
                                                 jira_cfg["ticket"], cases)
            print(f"  [Functional Cases] stored in Jira {jira_cfg['ticket']} via {jira_result.get('method')} "
                  f"({len(jira_result.get('created', []))} issue(s))")
        except Exception as exc:
            print(f"  [Functional Cases] could not write to Jira: {exc}")

    print(f"  [Functional Cases] derived {len(cases)} test case(s) from the acceptance criteria")
    return {"functional_cases": cases, "functional_artifacts": arts, "jira_cases": jira_result}
