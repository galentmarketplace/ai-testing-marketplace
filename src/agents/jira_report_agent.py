"""Jira Report agent — closes the loop by posting the run RESULTS back onto the Jira ticket.

Runs last (after the PR). Comments the outcome on the ticket that started the run: overall status,
the functional cases (and the sub-tasks they were stored as), CI result, the PR link, and what the
run was grounded against — so Jira reflects the whole ticket → production journey.
"""
from ..state import PipelineState


def _final_gates(state: PipelineState) -> dict:
    out = {}
    for g in state.get("gate_decisions", []):
        out[g["gate"]] = g["verdict"]
    return out


def jira_report(state: PipelineState) -> dict:
    cfg = state.get("run_config", {}) or {}
    jira_cfg = cfg.get("jira")
    if not (jira_cfg and jira_cfg.get("ticket")):
        return {}   # not a Jira-driven run — nothing to post

    from ..integration import jira
    inp = state.get("story", {}).get("inputs", {}) or {}
    analysis = state.get("repo_analysis") or {}
    cases = state.get("functional_cases") or []
    jc = state.get("jira_cases") or {}
    jr = state.get("jenkins_report") or {}
    pr = state.get("pr") or {}
    gates = _final_gates(state)

    auto = sum(1 for c in cases if c.get("automatable", True))
    blocked = state.get("status") == "blocked"
    lines = [
        "🤖 *AI Testing Marketplace* — automated run complete",
        f"Result: {'⚠ Blocked — human review needed' if blocked else '✅ Automation green — PR ready to merge'}",
        "",
    ]
    if cases:
        stored = f" — stored as {len(jc.get('created', []))} sub-task(s): {', '.join(jc.get('created', []))}" if jc.get("created") else ""
        lines.append(f"Functional test cases: {len(cases)} ({auto} automatable, {len(cases) - auto} manual){stored}")
    if jr.get("status") == "ran":
        url = (jr.get("runs") or [{}])[-1].get("url", "")
        lines.append(f"CI (Jenkins): {jr.get('overall')} — {jr.get('passed', 0)} passed, {jr.get('failed', 0)} failed"
                     + (f"  {url}" if url else ""))
    if pr.get("url", "").startswith("http"):
        lines.append(f"Pull request: {pr['url']}")
    if gates:
        lines.append("Quality gates: " + ", ".join(f"{g} {'✓' if v == 'pass' else '✗'}" for g, v in gates.items()))
    grounded = []
    if inp.get("source_repo"):
        grounded.append(f"dev repo {inp['source_repo']}" + (f" (login: {analysis['login_route']})" if analysis.get("login_route") else ""))
    if inp.get("base_url"):
        grounded.append(f"app {inp['base_url']}")
    if grounded:
        lines.append("Grounded against: " + " · ".join(grounded) + ".")

    try:
        jira.add_comment(jira_cfg["url"], jira_cfg["email"], jira_cfg["token"], jira_cfg["ticket"], "\n".join(lines))
        print(f"  [Jira Report] posted results to {jira_cfg['ticket']}")
        return {"jira_reported": True}
    except Exception as exc:
        print(f"  [Jira Report] could not post to Jira: {exc}")
        return {"jira_reported": False}
