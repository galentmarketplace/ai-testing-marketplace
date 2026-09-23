"""MCP server — plug the AI Testing Marketplace into coding agents (Claude Code, Cursor, …).

The agent-native distribution pattern: instead of a human clicking a UI, a coding agent gets the
marketplace as TOOLS — list playbooks/agents, start a run against a saved Configuration + Jira ticket,
poll status, and read the folded results (gates, PR, Jenkins). "Verify as you build."

Run (stdio):   ATM_TOKEN=atm_... python -m src.mcp_server
Register:      claude mcp add ai-testing-marketplace -e ATM_TOKEN=atm_... -- python -m src.mcp_server
Env:           ATM_URL   (default http://127.0.0.1:8090) — the running marketplace backend.
               ATM_TOKEN — an API token minted in the dashboard (Configuration → API tokens), or the
                           deployment's ATM_API_TOKEN service token. Every call goes through the
                           authenticated REST API; this process never touches the database.
"""
import json
import os
import urllib.error
import urllib.request

from mcp.server.fastmcp import FastMCP

ATM = os.environ.get("ATM_URL", "http://127.0.0.1:8090").rstrip("/")
TOKEN = os.environ.get("ATM_TOKEN", "")


def _headers() -> dict:
    h = {"Content-Type": "application/json"}
    if TOKEN:
        h["Authorization"] = f"Bearer {TOKEN}"
    return h
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


def _call(path: str, body: dict | None = None, method: str = "GET"):
    req = urllib.request.Request(ATM + path, data=json.dumps(body).encode() if body is not None else None,
                                 headers=_headers(), method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        if e.code == 401:
            return {"error": "unauthorized — set ATM_TOKEN to an API token (dashboard → Configuration → API tokens)"}
        return {"error": f"HTTP {e.code} on {path}: {e.read().decode()[:300]}"}


def _get(path: str):
    return _call(path)


def _post(path: str, body: dict):
    return _call(path, body, "POST")


@mcp.tool()
def list_playbooks() -> dict:
    """Ready-made testing flows you can start (id, what it does)."""
    return {k: v["desc"] for k, v in PLAYBOOKS.items()}


@mcp.tool()
def list_agents() -> list:
    """Every agent capability in the marketplace (id, label, category, what it produces)."""
    r = _get("/api/manifest")
    if "error" in r:
        return [r]
    return [{k: a.get(k) for k in ("id", "label", "category", "kind", "produces")} for a in r.get("agents", [])]


@mcp.tool()
def list_configurations() -> list:
    """Saved Configurations (Jira + app + repos + Jenkins; secrets redacted) usable as a run target."""
    r = _get("/api/projects")
    if "error" in r:
        return [r]          # surface "unauthorized — set ATM_TOKEN" instead of an empty list
    return [{"id": p["id"], "name": p["name"], "app_url": p["config"].get("app_url"),
             "source_repo": p["config"].get("source_repo"), "jira": bool(p["config"].get("jira_url"))}
            for p in r.get("projects", [])]


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
    r = _get(f"/api/runs/{run_id}")
    if "error" in r:
        return r
    return {k: r.get(k) for k in ("id", "status", "flow", "mode", "created_at", "updated_at", "tracks")}


@mcp.tool()
def run_results(run_id: str) -> dict:
    """Folded results: gate verdicts (+checks), PR url, Jenkins result, functional cases, security and
    coverage summaries, artifacts, Jira report — computed server-side under the caller's authorization."""
    return _get(f"/api/runs/{run_id}/summary")


if __name__ == "__main__":
    mcp.run()
