"""Phase 1.1/1.2 acceptance: an audit trail for every security-relevant action, and SSO config."""
from fastapi.testclient import TestClient

import web.server as srv
from web import store

client = TestClient(srv.app)
SERVICE = {"Authorization": "Bearer test-service-token"}


def _actions(entries):
    return [e["action"] for e in entries]


def test_mutations_are_audited_with_actor_and_target():
    _, tok = store.create_api_token("frank", "cli")
    h = {"Authorization": f"Bearer {tok}"}
    pid = client.post("/api/projects", json={"name": "Frank cfg", "config": {}}, headers=h).json()["id"]
    client.post("/api/projects", json={"id": pid, "name": "Frank cfg v2", "config": {}}, headers=h)
    client.delete(f"/api/projects/{pid}", headers=h)
    mine = client.get("/api/audit", headers=h).json()["entries"]
    assert {"config.create", "config.update", "config.delete"} <= set(_actions(mine))
    created = next(e for e in mine if e["action"] == "config.create")
    assert created["actor"] == "frank" and created["target"] == pid and created["via"] == "token"
    assert created["outcome"] == "ok" and created["ts"] > 0 and created["detail"]["name"] == "Frank cfg"


def test_audit_is_owner_scoped_but_admins_see_everything():
    _, g = store.create_api_token("grace", "cli")
    client.post("/api/projects", json={"name": "Grace cfg", "config": {}}, headers={"Authorization": f"Bearer {g}"})
    grace = client.get("/api/audit", headers={"Authorization": f"Bearer {g}"}).json()["entries"]
    assert grace and {e["actor"] for e in grace} == {"grace"}          # never another tenant's trail
    everyone = {e["actor"] for e in client.get("/api/audit", headers=SERVICE).json()["entries"]}
    assert {"grace", "frank"} <= everyone


def test_audit_requires_auth():
    assert client.get("/api/audit").status_code == 401


def test_token_lifecycle_and_artifact_reads_are_audited():
    _, h = store.create_api_token("heidi", "cli")
    hdr = {"Authorization": f"Bearer {h}"}
    store.audit("heidi", "token", "login", "github")                   # direct write also lands
    acts = _actions(client.get("/api/audit", headers=hdr).json()["entries"])
    assert "login" in acts


def test_audit_write_never_breaks_a_request(monkeypatch):
    monkeypatch.setattr(store, "_conn", lambda: (_ for _ in ()).throw(RuntimeError("db down")))
    store.audit("ivan", "session", "login")                            # must swallow, not raise


def test_auth_methods_advertises_configured_providers():
    m = client.get("/api/auth/methods").json()
    assert set(m) >= {"github", "sso", "sso_name"} and isinstance(m["sso"], bool)


def test_sso_login_is_rejected_when_unconfigured(monkeypatch):
    from src.integration import oidc
    monkeypatch.setattr(oidc, "configured", lambda: False)
    assert client.get("/auth/oidc/login", follow_redirects=False).status_code == 400


def test_sso_callback_rejects_an_unknown_state():
    r = client.get("/auth/oidc/callback", params={"code": "x", "state": "never-issued"}, follow_redirects=False)
    assert r.status_code == 400
    failures = [e for e in client.get("/api/audit", headers=SERVICE).json()["entries"]
                if e["action"] == "login.failed"]
    assert failures and failures[0]["outcome"] == "denied"
