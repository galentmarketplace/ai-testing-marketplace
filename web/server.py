"""Web UI backend for the Agentic Testing Pipeline.

Runs the *real* LangGraph pipeline and streams every agent/gate step to the
browser over Server-Sent Events (SSE), so you can watch the pipeline execute
node by node.

Run it:
    # from the agentic-testing-pipeline/ directory, with the venv active
    MOCK_LLM=1 python -m web.server            # demo, no API key needed
    python -m web.server                       # real Claude (needs .env)

Then open http://127.0.0.1:8000
"""
import hmac
import json
import os
import secrets
import threading
import time
import uuid
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from pydantic import BaseModel

from src import observability, runctx, sandbox
from src.config import GENERATED_DIR
from src.graph import build_graph
from src.integration import github as gh
from src.integration import jira
from src.main import DEFAULT_STORY
from src.orchestrator import orchestrate
from src.registry import build_manifest
from web import store

observability.setup_logging()   # JSON logs tagged with the active run_id
app = FastAPI(title="Agentic Testing Pipeline")

STATIC_DIR = Path(__file__).parent / "static"

# ---------- Persistence (SQLite) ----------
store.init_db()
_interrupted = store.mark_interrupted()   # any run left 'running' by a prior restart
if _interrupted:
    print(f"  [store] marked {_interrupted} orphaned run(s) as interrupted")

# ---------- GitHub OAuth (per-user login) ----------
SESSIONS: dict[str, dict] = {}   # sid -> {token, login, avatar} (in-memory cache of the sessions table)
for _s in store.all_sessions():   # restore logins across restarts
    SESSIONS[_s["sid"]] = {"token": _s["token"], "login": _s["login"], "avatar": _s["avatar"]}
OAUTH_STATES: set[str] = set()
# Authorization model (Phase 0.1): every /api/* route needs a principal — a browser session OR a bearer
# API token. Admins (ATM_ADMIN_LOGINS) see every row, incl. legacy owner-less ones. ATM_API_TOKEN is an
# optional bootstrap service token for headless deployments (CI, the MCP server) and counts as admin.
ADMINS = {x.strip() for x in os.environ.get("ATM_ADMIN_LOGINS", "").split(",") if x.strip()}
ENV_TOKEN = os.environ.get("ATM_API_TOKEN") or None
SERVICE_LOGIN = os.environ.get("ATM_SERVICE_LOGIN", "service")
# Whole-run deadline: a runaway run used to end only at MAX_ITERS or a 30-minute Jenkins poll.
RUN_DEADLINE_S = float(os.environ.get("ATM_RUN_DEADLINE_S", "2700"))   # 45 minutes
PUBLIC_URL = os.environ.get("PUBLIC_URL", "")   # e.g. https://xxx.ngrok-free.dev (fixes the OAuth callback)


def _redirect_uri(request: Request) -> str:
    base = (PUBLIC_URL or str(request.base_url)).rstrip("/")
    return base + "/auth/github/callback"


def _session(request: Request) -> dict | None:
    sid = request.cookies.get("sid")
    return SESSIONS.get(sid) if sid else None


def _principal(request: Request) -> dict | None:
    """Who is calling: {login, via, admin, token?}. Session cookie first, then `Authorization: Bearer`."""
    sess = _session(request)
    if sess and sess.get("login"):
        return {"login": sess["login"], "via": "session", "admin": sess["login"] in ADMINS, "token": sess.get("token")}
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        presented = auth[7:].strip()
        if ENV_TOKEN and hmac.compare_digest(presented, ENV_TOKEN):
            return {"login": SERVICE_LOGIN, "via": "env", "admin": True, "token": None}
        row = store.resolve_api_token(presented)
        if row:
            return {"login": row["login"], "via": "token", "admin": row["login"] in ADMINS, "token": None}
    return None


def require_auth(request: Request) -> dict:
    p = _principal(request)
    if not p:
        raise HTTPException(401, "authentication required: sign in, or send Authorization: Bearer <API token>")
    return p


def _owned(owner_login: str | None, principal: dict) -> bool:
    return principal["admin"] or (owner_login is not None and owner_login == principal["login"])


def _assert_owner_project(project_id: str, principal: dict) -> None:
    exists, owner = store.project_owner(project_id)
    if not exists or not _owned(owner, principal):
        raise HTTPException(404, "project not found")     # never reveal another tenant's ids


def _assert_owner_run(run_id: str, principal: dict) -> dict:
    r = store.get_run(run_id)
    if not r or not _owned(r.get("login"), principal):
        raise HTTPException(404, "run not found")
    return r


@app.get("/auth/github/login")
def gh_login(request: Request):
    if not gh.oauth_configured():
        raise HTTPException(400, "GitHub OAuth not configured (set GITHUB_CLIENT_ID / GITHUB_CLIENT_SECRET)")
    state = secrets.token_urlsafe(16)
    OAUTH_STATES.add(state)
    return RedirectResponse(gh.oauth_authorize_url(_redirect_uri(request), state))


@app.get("/auth/github/callback")
def gh_callback(request: Request, code: str = "", state: str = ""):
    if state not in OAUTH_STATES:
        raise HTTPException(400, "invalid OAuth state")
    OAUTH_STATES.discard(state)
    token = gh.oauth_exchange(code, _redirect_uri(request))
    if not token:
        raise HTTPException(400, "GitHub token exchange failed")
    gh.set_token(token)
    me = gh.whoami()
    sid = secrets.token_urlsafe(24)
    SESSIONS[sid] = {"token": token, "login": me.get("login"), "avatar": me.get("avatar")}
    store.save_session(sid, token, me.get("login"), me.get("avatar"))   # survive restarts
    resp = RedirectResponse("/")
    resp.set_cookie("sid", sid, httponly=True, max_age=86400, samesite="lax",
                    secure=PUBLIC_URL.startswith("https"))   # Secure whenever we're served over TLS
    return resp


@app.get("/auth/logout")
def gh_logout(request: Request):
    sid = request.cookies.get("sid", "")
    SESSIONS.pop(sid, None)
    store.delete_session(sid)
    resp = RedirectResponse("/")
    resp.delete_cookie("sid")
    return resp

# UI graph metadata is DERIVED from the agent registry (single source of truth).
# The manifest (lane columns + backend→display routes) is streamed to the browser
# so a newly-registered agent appears in the graph with no frontend edits.
MANIFEST = build_manifest()
NODE_META = {
    **MANIFEST["node_meta"],
    # legacy nodes used only by the fixed LangGraph path (src/graph.py)
    "generate_scripts": {"label": "UI + Perf Agents", "kind": "agent", "emits": "test_artifacts"},
    "analyze_failure":  {"label": "Analyze Failure",  "kind": "gate",  "emits": "status"},
    "blocked":          {"label": "Blocked",          "kind": "gate",  "emits": "status"},
}


class RunRequest(BaseModel):
    story: dict | None = None
    mock: bool = True
    step_delay: float = 0.6  # artificial pause so the animation is watchable
    agentic: bool = True     # True = autonomous reasoning orchestrator; False = fixed LangGraph
    fail_gate: str | None = None  # demo: force a gate to fail once — "unit" | "feature" | "regression"
                                  # (or "UNIT"/"QG1"/"QG2") to watch the self-correction loop-back
    mode: str = "full"       # intake scope — "full" | "regression" | "perf" | "smoke" | "custom"
    tracks: list[str] | None = None  # for mode="custom": hand-picked tracks (from the flow builder)
    open_pr: bool | None = None      # custom builder: whether to open a PR after all gates pass
    entry: str | None = None         # "from_existing_code" (test a repo) | "from_story" (build a feature)
    ac_input: str | None = None      # Acceptance Criteria agent source: a Jira link or pasted criteria
    inputs: dict | None = None       # tailored per-flow intake (repo, scope_prompt, base_url, ci_url, ...)
    project_id: str | None = None    # run from a saved Project — its config prefills inputs (secrets decrypted server-side)
    ticket: str | None = None        # a Jira issue key — the platform pulls its acceptance criteria live
    jira: dict | None = None         # server-set transient Jira connection (NOT persisted) — lets agents write cases back


# Secrets never leave process memory: everything persisted to the store or streamed to a browser passes
# through here. Agents still receive the real values via the in-memory config/inputs.
_SECRET_KEYS = {"token", "github_token", "jira_token", "login_password", "jenkins_token", "password",
                "secret", "client_secret", "api_key"}


def _redact(o):
    if isinstance(o, dict):
        return {k: ("***" if (k in _SECRET_KEYS and isinstance(v, str) and v) else _redact(v)) for k, v in o.items()}
    if isinstance(o, list):
        return [_redact(v) for v in o]
    return o


def _sse(event: str, data: dict) -> str:
    data = _redact(data)  # never stream secrets
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


def _pipeline_events(req: RunRequest):
    """Generator yielding SSE frames as the LangGraph pipeline executes."""
    # MOCK_LLM is read per-call inside src.llm, so setting it here is enough.
    runctx.set_run_context(mock=req.mock)

    story = req.story or DEFAULT_STORY
    yield _sse("start", {"story": story, "nodes": NODE_META, "mock": req.mock})

    graph = build_graph()
    state = {"story": story, "status": "running", "attempts": {}}

    try:
        for chunk in graph.stream(state, config={"recursion_limit": 50},
                                  stream_mode="updates"):
            # chunk == {node_name: partial_state_update}
            for node, update in chunk.items():
                meta = NODE_META.get(node, {"label": node, "kind": "agent"})
                if req.step_delay:
                    time.sleep(req.step_delay)
                yield _sse("node", {
                    "node": node,
                    "label": meta["label"],
                    "kind": meta["kind"],
                    "update": update,
                })
        yield _sse("done", {"ok": True})
    except Exception as exc:  # surface pipeline errors to the UI
        yield _sse("error", {"message": str(exc)})


def _execute_run(run_id: str, req: RunRequest, github_token: str | None):
    """Run the orchestrator to completion IN THE BACKGROUND, persisting every event to the
    store. Decoupled from any browser connection, so a refresh/disconnect never aborts it."""
    # Per-run context (NOT process env): mock/fail-gate demo knobs are bound to THIS worker thread only,
    # so a mock run can never flip a concurrent live run into mock (or vice versa).
    _GATE_TO_SUITE = {"unit": "unit", "UNIT": "unit", "feature": "feature", "QG1": "feature",
                      "regression": "regression", "QG2": "regression", "all": "all"}
    val = _GATE_TO_SUITE.get(req.fail_gate) if req.fail_gate else None
    runctx.set_run_context(mock=req.mock, fail_suite=val, run_id=run_id)

    story = req.story or DEFAULT_STORY
    if req.inputs:
        story = {**story, "inputs": req.inputs}
    if req.ac_input:
        story = {**story, "ac_input": req.ac_input}
    config = {"mode": req.mode}
    if req.tracks:
        config["tracks"] = req.tracks
    if req.open_pr is not None:
        config["open_pr"] = req.open_pr
    if req.entry:
        config["entry"] = req.entry
    if github_token:
        config["github_token"] = github_token
    if req.jira:
        config["jira"] = req.jira   # transient — lets the Functional Case agent store cases back in Jira

    seq = 0
    store.append_event(run_id, seq, "start", _redact({"story": story, "nodes": NODE_META, "mock": req.mock,
                                              "agentic": True, "config": config, "manifest": MANIFEST}))
    seq += 1
    store.set_status(run_id, "running")
    final = "done"
    try:
        for ev in orchestrate(story, config=config,
                              should_cancel=lambda: store.cancel_requested(run_id),
                              deadline_s=RUN_DEADLINE_S):
            kind = ev["type"]
            if kind == "think":
                payload = {"reasoning": ev["reasoning"], "next": ev["next"], "iteration": ev["iteration"]}
            elif kind == "node":
                payload = {"node": ev["node"], "label": ev["label"], "kind": ev["kind"], "update": ev["update"]}
            elif kind == "done":
                final = ev.get("status", "done")
                payload = {"ok": True, "status": final}
            else:
                payload = ev
            store.append_event(run_id, seq, kind, _redact(payload))
            seq += 1
        usage = observability.usage_for(run_id)
        observability.log("atm.run", "run finished", status=final,
                          **{k: usage[k] for k in ("calls", "input_tokens", "output_tokens", "cost_usd")})
        store.finish_run(run_id, final, {"status": final, "llm_usage": usage})
        observability.clear_usage(run_id)
    except Exception as exc:  # persist the error so a reconnecting client sees it
        store.append_event(run_id, seq, "error", {"message": str(exc)})
        store.finish_run(run_id, "error", {"message": str(exc)})


@app.post("/api/run")
def run(req: RunRequest, request: Request, principal: dict = Depends(require_auth)):
    """Start a run and return its id immediately. The run executes in the background and
    streams via GET /api/runs/{id}/stream (reconnectable). Legacy fixed-graph path still
    streams inline."""
    token = principal.get("token")            # GitHub OAuth token — session principals only
    login = principal["login"]
    if not req.agentic:
        return StreamingResponse(_pipeline_events(req), media_type="text/event-stream")

    # Run from a saved Project: its config prefills the inputs (secrets decrypted here, never client-side),
    # and a Jira ticket key pulls the acceptance criteria LIVE from Jira (the source of truth).
    if req.project_id:
        _assert_owner_project(req.project_id, principal)
        p = store.get_project(req.project_id, reveal=True) or {}
        inp = dict(req.inputs or {})
        _MAP = {"base_url": "app_url", "login_url": "login_url", "login_user": "login_user",
                "login_password": "login_password", "source_repo": "source_repo", "dest_repo": "dest_repo",
                "jenkins_ui_job": "jenkins_job"}
        for ikey, pkey in _MAP.items():
            if not inp.get(ikey) and p.get(pkey):
                inp[ikey] = p[pkey]
        if p.get("source_repo") and not inp.get("repo"):
            inp["repo"] = "https://github.com/" + p["source_repo"]
        req.inputs = inp
        if req.ticket and p.get("jira_url") and p.get("jira_email") and p.get("jira_token"):
            try:
                issue = jira.fetch_issue(p["jira_url"], p["jira_email"], p["jira_token"], req.ticket)
                req.ac_input = req.ac_input or issue["ac_text"]
                req.story = {**(req.story or {}), "id": issue["key"], "title": issue["summary"]}
                # transient Jira connection so agents can write test cases BACK to the ticket (Jira = system of record)
                req.jira = {"url": p["jira_url"], "email": p["jira_email"], "token": p["jira_token"], "ticket": req.ticket}
            except Exception as exc:
                raise HTTPException(400, f"Could not fetch Jira ticket {req.ticket}: {exc}")

    run_id = uuid.uuid4().hex
    flow = (req.story or {}).get("title") or req.mode
    store.create_run(run_id, login, flow, req.mode, req.tracks, _redact(req.inputs), _redact(req.story))
    threading.Thread(target=_execute_run, args=(run_id, req, token), daemon=True).start()
    return {"run_id": run_id, "status": "running"}


@app.get("/api/runs/{run_id}/stream")
def run_stream(run_id: str, principal: dict = Depends(require_auth)):
    """Replay all stored events for a run, then tail live until it reaches a terminal state.
    This is what makes refresh/reconnect (and watching an already-finished run) work."""
    _assert_owner_run(run_id, principal)
    def gen():
        last = -1
        while True:
            evs = store.events_after(run_id, last)
            for e in evs:
                yield _sse(e["type"], json.loads(e["payload"]))
                last = e["seq"]
            status = store.run_status(run_id)
            if status is None:
                yield _sse("error", {"message": "run not found"})
                return
            if status in store.TERMINAL:
                for e in store.events_after(run_id, last):   # flush any final events
                    yield _sse(e["type"], json.loads(e["payload"]))
                    last = e["seq"]
                if status == "interrupted":
                    yield _sse("done", {"ok": False, "status": "interrupted"})
                return
            time.sleep(0.35)
    return StreamingResponse(gen(), media_type="text/event-stream")


# ---------- Projects (saved, reusable per-project config) ----------
class ProjectReq(BaseModel):
    id: str | None = None
    name: str
    config: dict = {}


@app.get("/api/projects")
def projects_list(principal: dict = Depends(require_auth)):
    return {"projects": store.list_projects(principal["login"], admin=principal["admin"])}


@app.get("/api/projects/{project_id}")
def project_get(project_id: str, principal: dict = Depends(require_auth)):
    _assert_owner_project(project_id, principal)
    p = store.get_project(project_id)   # redacted (no secrets)
    if not p:
        raise HTTPException(404, "project not found")
    return p


@app.post("/api/projects")
def project_save(req: ProjectReq, principal: dict = Depends(require_auth)):
    if req.id:
        _assert_owner_project(req.id, principal)
    pid = store.save_project(principal["login"], req.name, req.config, req.id)
    return {"id": pid, **(store.get_project(pid) or {})}


@app.delete("/api/projects/{project_id}")
def project_delete(project_id: str, principal: dict = Depends(require_auth)):
    _assert_owner_project(project_id, principal)
    store.delete_project(project_id)
    return {"ok": True}


# ---------- Jira (live requirements source) ----------
class JiraTestReq(BaseModel):
    jira_url: str
    jira_email: str
    jira_token: str


@app.post("/api/jira/test")
def jira_test(req: JiraTestReq, principal: dict = Depends(require_auth)):
    try:
        return jira.test_connection(req.jira_url, req.jira_email, req.jira_token)
    except Exception as exc:
        raise HTTPException(400, f"Jira connection failed: {exc}")


@app.get("/api/jira/issues")
def jira_issues(project_id: str, project_key: str, principal: dict = Depends(require_auth)):
    _assert_owner_project(project_id, principal)
    p = store.get_project(project_id, reveal=True) or {}
    if not (p.get("jira_url") and p.get("jira_email") and p.get("jira_token")):
        raise HTTPException(400, "project has no Jira connection configured")
    try:
        return {"issues": jira.list_issues(p["jira_url"], p["jira_email"], p["jira_token"], project_key)}
    except Exception as exc:
        raise HTTPException(400, f"Jira issue list failed: {exc}")


@app.get("/api/jira/issue")
def jira_issue(project_id: str, key: str, principal: dict = Depends(require_auth)):
    _assert_owner_project(project_id, principal)
    p = store.get_project(project_id, reveal=True) or {}
    if not (p.get("jira_url") and p.get("jira_email") and p.get("jira_token")):
        raise HTTPException(400, "project has no Jira connection configured")
    try:
        return jira.fetch_issue(p["jira_url"], p["jira_email"], p["jira_token"], key)
    except Exception as exc:
        raise HTTPException(400, f"Jira fetch failed: {exc}")


@app.get("/api/runs")
def runs(principal: dict = Depends(require_auth)):
    """Recent run history for the caller (admins see every run)."""
    return {"runs": store.list_runs(principal["login"], admin=principal["admin"])}


@app.get("/api/runs/{run_id}")
def run_detail(run_id: str, principal: dict = Depends(require_auth)):
    return _assert_owner_run(run_id, principal)


def _fold_run(run_id: str) -> dict:
    """Fold a run's persisted (already-redacted) events into one results object — the same shape the
    MCP server and the extension consume, computed server-side so callers never touch the store."""
    r = store.get_run(run_id) or {}
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
              "checks": [{"label": c["label"], "ok": c["ok"], **({"advisory": True} if c.get("advisory") else {})}
                         for c in g.get("checks", [])]} for g in st.get("gate_decisions", [])]
    arts = [a.get("path") for k in ("test_artifacts", "security_artifacts", "functional_artifacts", "coverage_artifacts")
            for a in st.get(k, []) if a.get("path")]
    sec = st.get("security_report")
    usage = (r.get("result") or {}).get("llm_usage") or observability.usage_for(run_id)
    return {"run_id": run_id, "status": r.get("status"), "flow": r.get("flow"), "agents_run": nodes, "gates": gates,
            "llm_usage": usage, "step_errors": st.get("step_errors"),
            "pr": (st.get("pr") or {}).get("url"), "jenkins": st.get("jenkins_report"),
            "functional_cases": len(st.get("functional_cases") or []), "jira_cases": st.get("jira_cases"),
            "security": {k: sec.get(k) for k in ("total", "by_severity", "methodologies_run")} if sec else None,
            "coverage": {k: (st.get("coverage_report") or {}).get(k) for k in ("total_pct", "min_pct", "uncovered_funcs")}
                        if st.get("coverage_report") else None,
            "artifacts": arts[:40], "jira_reported": st.get("jira_reported")}


@app.post("/api/runs/{run_id}/cancel")
def run_cancel(run_id: str, principal: dict = Depends(require_auth)):
    """Ask a running run to stop. The orchestrator checks between steps, so the run ends at the next
    boundary with status `cancelled` (already-finished runs are returned unchanged)."""
    r = _assert_owner_run(run_id, principal)
    if r.get("status") in store.TERMINAL:
        return {"ok": True, "status": r["status"], "note": "run already finished"}
    store.request_cancel(run_id)
    return {"ok": True, "status": "cancelling"}


@app.get("/api/runs/{run_id}/summary")
def run_summary(run_id: str, principal: dict = Depends(require_auth)):
    """Folded results for a run (gates + checks, PR, Jenkins, cases, security, coverage, artifacts)."""
    _assert_owner_run(run_id, principal)
    return _fold_run(run_id)


@app.get("/api/artifact")
def artifact(path: str, principal: dict = Depends(require_auth)):
    """Return the content of a generated artifact file (sandboxed to generated/)."""
    target = Path(path).resolve()
    gen_root = GENERATED_DIR.resolve()
    if gen_root not in target.parents:
        raise HTTPException(403, "path outside generated/")
    if not target.is_file():
        raise HTTPException(404, "not found")
    # Artifacts live under generated/runs/<run_id>/… — only the run's owner (or an admin) may read them.
    owner_run = sandbox.run_id_for_path(target)
    if owner_run:
        _assert_owner_run(owner_run, principal)
    elif not principal["admin"]:
        raise HTTPException(404, "not found")      # legacy shared-directory artifacts: admin only
    return {"path": str(target), "content": target.read_text()}


# ---------- Identity + API tokens ----------
@app.get("/api/me")
def me(principal: dict = Depends(require_auth)):
    return {"login": principal["login"], "via": principal["via"], "admin": principal["admin"]}


class TokenReq(BaseModel):
    name: str = "token"


@app.post("/api/tokens")
def token_create(req: TokenReq, principal: dict = Depends(require_auth)):
    """Mint a bearer API token for the signed-in user (browser session only — a token can't mint tokens).
    The plaintext is returned exactly once."""
    if principal["via"] != "session":
        raise HTTPException(403, "API tokens can only be created from a signed-in browser session")
    tid, plain = store.create_api_token(principal["login"], req.name)
    return {"id": tid, "name": req.name, "token": plain}


@app.get("/api/tokens")
def token_list(principal: dict = Depends(require_auth)):
    return {"tokens": store.list_api_tokens(principal["login"])}


@app.delete("/api/tokens/{token_id}")
def token_delete(token_id: str, principal: dict = Depends(require_auth)):
    if not store.delete_api_token(principal["login"], token_id):
        raise HTTPException(404, "token not found")
    return {"ok": True}


@app.get("/api/default-story")
def default_story():
    return DEFAULT_STORY


@app.get("/api/manifest")
def manifest():
    """The registry-derived UI manifest (lanes, routes, track catalog) — used by the
    Custom flow builder before any run has streamed a `start` event."""
    return MANIFEST


@app.get("/api/github/status")
def github_status(request: Request):
    """Whether GitHub is connected for THIS user (session), and as whom."""
    sess = _session(request)
    if sess:
        return {"connected": True, "login": sess["login"], "avatar": sess["avatar"], "oauth": True}
    if gh.oauth_configured():
        return {"connected": False, "oauth": True}   # OAuth on → user must log in
    gh.set_token(None)                                # no OAuth → single-user PAT fallback
    if gh.connected():
        try:
            return {"connected": True, **gh.whoami(), "oauth": False}
        except Exception as exc:
            return {"connected": False, "oauth": False, "error": str(exc)}
    return {"connected": False, "oauth": False}


@app.get("/api/github/repos")
def github_repos(request: Request):
    """The signed-in user's repos for the source/destination pickers."""
    sess = _session(request)
    if not sess and gh.oauth_configured():
        return {"repos": []}                          # must log in first
    gh.set_token(sess["token"] if sess else None)     # None → PAT fallback
    if not gh.connected():
        return {"repos": []}
    try:
        return {"repos": gh.list_repos()}
    except Exception as exc:
        return {"repos": [], "error": str(exc)}


@app.get("/", response_class=HTMLResponse)
def index():
    return (STATIC_DIR / "index.html").read_text()


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", "8000"))
    print(f"\n  Agentic Testing Pipeline UI → http://127.0.0.1:{port}\n")
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
