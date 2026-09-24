"""Phase 0.5/0.6 acceptance: runs can be cancelled, hit a deadline, and a crashed step blocks."""

from fastapi.testclient import TestClient

import web.server as srv
from src import orchestrator
from src.orchestrator import orchestrate
from web import store

client = TestClient(srv.app)
SERVICE = {"Authorization": "Bearer test-service-token"}
STORY = {"id": "T", "title": "t", "description": "", "inputs": {}}
CFG = {"mode": "custom", "tracks": ["criteria", "functional"], "open_pr": False}


def _events(**kw):
    return list(orchestrate(STORY, dict(CFG), **kw))


def test_cancel_stops_before_the_next_step(monkeypatch):
    monkeypatch.setenv("MOCK_LLM", "1")
    calls = {"n": 0}

    def should_cancel():          # allow one dispatch, then ask to stop
        calls["n"] += 1
        return calls["n"] > 2

    ev = _events(should_cancel=should_cancel)
    assert ev[-1]["type"] == "done" and ev[-1]["status"] == "cancelled"
    assert any("Cancelled by the operator" in e.get("reasoning", "") for e in ev if e["type"] == "think")


def test_deadline_stops_the_run(monkeypatch):
    monkeypatch.setenv("MOCK_LLM", "1")
    ev = _events(deadline_s=-1)   # already past the deadline
    assert ev[-1]["status"] == "timed_out"


def test_crashed_step_blocks_instead_of_reporting_done(monkeypatch):
    monkeypatch.setenv("MOCK_LLM", "1")
    spec = orchestrator.BY_ID["generate_functional_cases"]

    def boom(state):
        raise RuntimeError("agent exploded")

    monkeypatch.setattr(spec, "fn", boom)
    ev = _events()
    assert ev[-1]["status"] == "blocked", "a crashed agent must not yield a done run"
    node = next(e for e in ev if e["type"] == "node" and e["node"] == "generate_functional_cases")
    assert node["update"]["step_status"] == "error" and "agent exploded" in node["update"]["error"]
    # the gate that depends on it must NOT have run
    assert not any(e["type"] == "node" and e["node"] == "quality_gate_1" for e in ev)


def test_cancel_endpoint_is_owner_scoped():
    store.create_run("run-cancelme", "alice", "f", "custom", [], {}, {"id": "S"})
    _, bob = store.create_api_token("bob2", "cli")
    assert client.post("/api/runs/run-cancelme/cancel", headers={"Authorization": f"Bearer {bob}"}).status_code == 404
    r = client.post("/api/runs/run-cancelme/cancel", headers=SERVICE)
    assert r.status_code == 200 and r.json()["status"] == "cancelling"
    assert store.cancel_requested("run-cancelme") is True
    store.finish_run("run-cancelme", "cancelled", {})
    assert client.post("/api/runs/run-cancelme/cancel", headers=SERVICE).json()["note"] == "run already finished"


def test_artifacts_are_owner_scoped(tmp_path):
    from src import runctx, sandbox
    store.create_run("run-art", "alice", "f", "custom", [], {}, {"id": "S"})
    tok = runctx.set_run_context(mock=True, run_id="run-art")
    f = sandbox.run_workspace("security") / "report.md"
    f.write_text("alice's findings")
    runctx.reset_run_context(tok)
    _, bob = store.create_api_token("bob3", "cli")
    assert client.get("/api/artifact", params={"path": str(f)}, headers={"Authorization": f"Bearer {bob}"}).status_code == 404
    r = client.get("/api/artifact", params={"path": str(f)}, headers=SERVICE)
    assert r.status_code == 200 and r.json()["content"] == "alice's findings"
