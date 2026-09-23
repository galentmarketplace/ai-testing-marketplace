"""Phase 0.1 acceptance: every /api route needs a principal; tenants cannot see each other's data."""
import sqlite3

import pytest
from fastapi.testclient import TestClient

import web.server as srv
from web import store

client = TestClient(srv.app)
SERVICE = {"Authorization": "Bearer test-service-token"}


@pytest.fixture(scope="module")
def tokens():
    _, alice = store.create_api_token("alice", "cli")
    _, bob = store.create_api_token("bob", "cli")
    return {"alice": {"Authorization": f"Bearer {alice}"}, "bob": {"Authorization": f"Bearer {bob}"}}


# ---- unauthenticated: sensitive routes reject, public metadata stays open ----
@pytest.mark.parametrize("path", ["/api/runs", "/api/runs/abc", "/api/runs/abc/stream", "/api/runs/abc/summary",
                                  "/api/projects", "/api/projects/abc", "/api/artifact?path=/etc/passwd",
                                  "/api/jira/issues?project_id=x&project_key=Y", "/api/jira/issue?project_id=x&key=Y",
                                  "/api/me", "/api/tokens"])
def test_unauthenticated_reads_are_rejected(path):
    assert client.get(path).status_code == 401


def test_unauthenticated_writes_are_rejected():
    assert client.post("/api/run", json={"mock": True}).status_code == 401
    assert client.post("/api/projects", json={"name": "x", "config": {}}).status_code == 401
    assert client.delete("/api/projects/abc").status_code == 401
    assert client.post("/api/jira/test", json={"jira_url": "u", "jira_email": "e", "jira_token": "t"}).status_code == 401


def test_public_metadata_stays_open():
    assert client.get("/api/manifest").status_code == 200
    assert client.get("/api/default-story").status_code == 200
    assert client.get("/api/github/status").status_code == 200


# ---- principals ----
def test_env_service_token_is_admin():
    r = client.get("/api/me", headers=SERVICE)
    assert r.status_code == 200 and r.json()["admin"] is True and r.json()["via"] == "env"


def test_garbage_bearer_is_rejected():
    assert client.get("/api/me", headers={"Authorization": "Bearer atm_not_a_real_token"}).status_code == 401


def test_bearer_cannot_mint_tokens(tokens):
    assert client.post("/api/tokens", json={"name": "x"}, headers=tokens["alice"]).status_code == 403


# ---- tenant isolation: projects ----
def test_project_isolation(tokens):
    r = client.post("/api/projects", json={"name": "Alice cfg", "config": {"app_url": "https://a.example"}}, headers=tokens["alice"])
    assert r.status_code == 200
    pid = r.json()["id"]
    assert client.get(f"/api/projects/{pid}", headers=tokens["alice"]).status_code == 200
    assert client.get(f"/api/projects/{pid}", headers=tokens["bob"]).status_code == 404      # not even existence
    assert client.delete(f"/api/projects/{pid}", headers=tokens["bob"]).status_code == 404
    assert client.post("/api/projects", json={"id": pid, "name": "hijack", "config": {}}, headers=tokens["bob"]).status_code == 404
    assert client.get("/api/jira/issues", params={"project_id": pid, "project_key": "X"}, headers=tokens["bob"]).status_code == 404
    names = [p["name"] for p in client.get("/api/projects", headers=tokens["alice"]).json()["projects"]]
    assert names == ["Alice cfg"]
    assert client.get("/api/projects", headers=tokens["bob"]).json()["projects"] == []
    admin_names = [p["name"] for p in client.get("/api/projects", headers=SERVICE).json()["projects"]]
    assert "Alice cfg" in admin_names
    assert client.get(f"/api/projects/{pid}", headers=SERVICE).status_code == 200


def test_legacy_ownerless_project_is_admin_only(tokens):
    pid = store.save_project(None, "legacy", {"app_url": "x"})
    assert client.get(f"/api/projects/{pid}", headers=tokens["alice"]).status_code == 404
    assert client.get(f"/api/projects/{pid}", headers=SERVICE).status_code == 200
    assert "legacy" not in [p["name"] for p in client.get("/api/projects", headers=tokens["alice"]).json()["projects"]]


# ---- tenant isolation: runs ----
def test_run_isolation(tokens):
    store.create_run("run-alice", "alice", "flow", "custom", ["unit"], {}, {"id": "S"})
    store.append_event("run-alice", 0, "node", {"node": "prescreen", "update": {"gate_decisions": []}})
    store.finish_run("run-alice", "done", {})
    assert client.get("/api/runs/run-alice", headers=tokens["alice"]).status_code == 200
    assert client.get("/api/runs/run-alice", headers=tokens["bob"]).status_code == 404
    assert client.get("/api/runs/run-alice/summary", headers=tokens["bob"]).status_code == 404
    assert client.get("/api/runs/run-alice/summary", headers=tokens["alice"]).json()["status"] == "done"
    assert client.get("/api/runs/run-alice", headers=SERVICE).status_code == 200
    ids = [r["id"] for r in client.get("/api/runs", headers=tokens["bob"]).json()["runs"]]
    assert "run-alice" not in ids


# ---- 0.2 core: OAuth tokens encrypted at rest ----
def test_session_token_encrypted_at_rest():
    store.save_session("sid-1", "gho_secret_value", "alice", "")
    raw = sqlite3.connect(store.DB_PATH).execute("SELECT token FROM sessions WHERE sid='sid-1'").fetchone()[0]
    assert raw.startswith("enc:") and "gho_secret_value" not in raw
    assert next(s for s in store.all_sessions() if s["sid"] == "sid-1")["token"] == "gho_secret_value"
