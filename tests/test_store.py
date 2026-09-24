"""Store: encryption at rest, redaction of project secrets, key rotation, run history."""
import sqlite3

from web import store


def test_project_secrets_encrypted_and_redacted():
    pid = store.save_project("carol", "cfg", {"jira_token": "tok-123", "login_password": "pw", "app_url": "https://a"})
    raw = sqlite3.connect(store.DB_PATH).execute("SELECT config FROM projects WHERE id=?", (pid,)).fetchone()[0]
    assert "tok-123" not in raw and "pw" not in raw and "enc:" in raw
    public = store.get_project(pid)
    assert "jira_token" not in public and public["has_jira_token"] is True and public["app_url"] == "https://a"
    assert store.get_project(pid, reveal=True)["jira_token"] == "tok-123"


def test_blank_secret_keeps_the_existing_value():
    pid = store.save_project("carol", "cfg2", {"jira_token": "keepme"})
    store.save_project("carol", "cfg2", {"jira_token": "", "app_url": "https://b"}, project_id=pid)
    assert store.get_project(pid, reveal=True)["jira_token"] == "keepme"


def test_rotation_preserves_every_secret():
    pid = store.save_project("carol", "rot", {"jira_token": "rot-secret"})
    store.save_session("sid-rot", "gho_rot", "carol", "")
    store.rotate_secrets()
    assert store.get_project(pid, reveal=True)["jira_token"] == "rot-secret"
    assert next(s for s in store.all_sessions() if s["sid"] == "sid-rot")["token"] == "gho_rot"


def test_run_history_is_owner_scoped():
    store.create_run("r-carol", "carol", "f", "custom", ["unit"], {"a": 1}, {"id": "S"})
    store.create_run("r-dave", "dave", "f", "custom", ["unit"], {}, {"id": "S"})
    assert [r["id"] for r in store.list_runs("carol")] == ["r-carol"]
    assert {"r-carol", "r-dave"} <= {r["id"] for r in store.list_runs(None, admin=True)}


def test_api_token_lifecycle():
    tid, plain = store.create_api_token("erin", "cli")
    assert plain.startswith("atm_")
    assert store.resolve_api_token(plain)["login"] == "erin"
    raw = sqlite3.connect(store.DB_PATH).execute("SELECT token_hash FROM api_tokens WHERE id=?", (tid,)).fetchone()[0]
    assert plain not in raw                                   # only the hash is stored
    assert store.delete_api_token("erin", tid) is True
    assert store.resolve_api_token(plain) is None
    assert store.delete_api_token("erin", tid) is False
