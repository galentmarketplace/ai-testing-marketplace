"""MCP server — plug the AI Testing Marketplace into coding agents (Claude Code, Cursor, …).

The agent-native distribution pattern: instead of a human clicking a UI, a coding agent gets the
marketplace as TOOLS — list playbooks/agents, start a run against a saved Configuration + Jira ticket,
poll status, and read the folded results (gates, PR, Jenkins). "Verify as you build."

Run (stdio):   python -m src.mcp_server
Register:      claude mcp add ai-testing-marketplace -- python -m src.mcp_server
Env:           ATM_URL (default http://127.0.0.1:8090) — the running marketplace backend.
"""
import json
import os
import sys
import urllib.request
from pathlib import Path

from mcp.server.fastmcp import FastMCP

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # so `web.store` resolves when run as a module
ATM = os.environ.get("ATM_URL", "http://127.0.0.1:8090").rstrip("/")
mcp = FastMCP("ai-testing-marketplace")

# Mirrors the Playbooks in the UI (flow id -> run mode/tracks).
PLAYBOOKS = {
    "full":       {"mode": "full", "desc": "story → criteria → code → unit, functional, perf, regression, security → PR"},
    "functional": {"mode": "custom", "tracks": ["criteria", "functional", "delivery", "jenkins"],
                   "desc": "Jira AC → functional cases → Playwright automation (DOM-grounded) → local heal → Jenkins → PR → results to Jira"},
    "perf":       {"mode": "perf", "desc": "open-model k6 (load/stress/soak/spike/breakpoint) + Core Web Vitals + per-pod CPU/mem on a deployed target"},
    "security":   {"mode": "custom", "tracks": ["security"], "desc": "multi-methodology scan: SAST, SCA/CVE, secrets, IaC, containers, SBOM → SARIF"},
    "regression": {"mode": "custom", "tracks": ["regression", "delivery", "jenkins"], "desc": "impacted regression cases in Jenkins, self-heal to green, PR"},
    "unit":       {"mode": "custom", "tracks": ["unit"], "desc": "run the repo's unit suite and gate"},
}


def _get(path: str):
    with urllib.request.urlopen(ATM + path, timeout=30) as r:
        return json.loads(r.read())


def _post(path: str, body: dict):
    req = urllib.request.Request(ATM + path, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


@mcp.tool()
def list_playbooks() -> dict:
    """Ready-made testing flows you can start (id, what it does)."""
    return {k: v["desc"] for k, v in PLAYBOOKS.items()}


@mcp.tool()
def list_agents() -> list:
    """Every agent capability in the marketplace (id, label, category, what it produces)."""
    return [{k: a.get(k) for k in ("id", "label", "category", "kind", "produces")}
            for a in _get("/api/manifest").get("agents", [])]


@mcp.tool()
def list_configurations() -> list:
    """Saved Configurations (Jira + app + repos + Jenkins; secrets redacted) usable as a run target."""
    return [{"id": p["id"], "name": p["name"], "app_url": p["config"].get("app_url"),
             "source_repo": p["config"].get("source_repo"), "jira": bool(p["config"].get("jira_url"))}
            for p in _get("/api/projects").get("projects", [])]


@mcp.tool()
def start_run(playbook: str = "functional", ticket: str | None = None, configuration_id: str | None = None,
              acceptance_criteria: str | None = None, inputs: dict | None = None, mock: bool = False) -> dict:
    """Start a run. playbook: full|functional|perf|security|regression|unit. Give a Jira `ticket` (AC pulled
    live) with a `configuration_id`, or paste `acceptance_criteria`. `inputs` = extra intake (repo, base_url,
    scope_prompt, perf_type, k8s_namespace, k8s_selector…). Returns the run_id to poll."""
    pb = PLAYBOOKS.get(playbook)
    if not pb:
        return {"error": f"unknown playbook '{playbook}'", "playbooks": list(PLAYBOOKS)}
    inp = dict(inputs or {})
    body = {"mock": mock, "mode": pb["mode"], "inputs": inp,
            "story": {"id": ticket or playbook.upper(), "title": f"{playbook} run",
                      "description": acceptance_criteria or inp.get("scope_prompt", "")}}
    if pb.get("tracks"):
        body["tracks"] = pb["tracks"]
        body["open_pr"] = "delivery" in pb["tracks"]
    if configuration_id:
        body["project_id"] = configuration_id
    if ticket:
        body["ticket"] = ticket
    if acceptance_criteria:
        body["ac_input"] = acceptance_criteria
    return _post("/api/run", body)


@mcp.tool()
def run_status(run_id: str) -> dict:
    """Current status of a run (queued|running|done|blocked|error) + its flow/mode/timestamps."""
    from web import store
    r = store.get_run(run_id)
    if not r:
        return {"error": "run not found"}
    return {k: r.get(k) for k in ("id", "status", "flow", "mode", "created_at", "updated_at", "tracks")}


@mcp.tool()
def run_results(run_id: str) -> dict:
    """Folded results: gate verdicts (+checks), PR url, Jenkins result, functional cases, artifacts, Jira report."""
    from web import store
    r = store.get_run(run_id)
    if not r:
        return {"error": "run not found"}
    st, nodes = {}, []
    for e in store.events_after(run_id, 0):
        try:
            p = json.loads(e["payload"]) if isinstance(e["payload"], str) else e["payload"]
        except Exception:
            continue
        if e["type"] == "node":
            nodes.append(p.get("node"))
            st.update(p.get("update") or {})
    gates = [{"gate": g["gate"], "verdict": g["verdict"], "reason": g.get("reason"),
              "checks": [{"label": c["label"], "ok": c["ok"]} for c in g.get("checks", [])]}
             for g in st.get("gate_decisions", [])]
    arts = [a.get("path") for k in ("test_artifacts", "security_artifacts", "functional_artifacts")
            for a in st.get(k, []) if a.get("path")]
    return {"status": r.get("status"), "agents_run": nodes, "gates": gates,
            "pr": (st.get("pr") or {}).get("url"), "jenkins": st.get("jenkins_report"),
            "functional_cases": len(st.get("functional_cases") or []),
            "security": {k: st["security_report"].get(k) for k in ("total", "by_severity", "methodologies_run")}
                        if st.get("security_report") else None,
            "artifacts": arts[:40], "jira_reported": st.get("jira_reported")}


if __name__ == "__main__":
    mcp.run()
