"""Persistence layer for the AI Testing Marketplace (SQLite, stdlib only).

Production-grade essentials without a heavy DB dependency:
  * runs execute in the background and every streamed event is durably stored, so a
    browser refresh (or restart) never loses progress — a client just re-attaches and
    replays from event 0, then tails live.
  * OAuth sessions are persisted, so logins survive a server restart.

SQLite in WAL mode handles our concurrency (background writer thread + SSE reader
threads). Connections are opened per call and closed promptly.
"""
import json
import sqlite3
import threading
import time
import uuid

from src.config import PROJECT_ROOT

DB_PATH = PROJECT_ROOT / "data" / "marketplace.db"
_KEY_PATH = PROJECT_ROOT / "data" / "secret.key"
_write_lock = threading.Lock()   # serialize writers (SQLite single-writer)

# Fields in a project config that are secrets — encrypted at rest, never returned to the UI.
SECRET_FIELDS = ("login_password", "jira_token", "jenkins_token")


def _fernet():
    from cryptography.fernet import Fernet
    if not _KEY_PATH.exists():
        _KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
        _KEY_PATH.write_bytes(Fernet.generate_key())
        try:
            _KEY_PATH.chmod(0o600)
        except Exception:
            pass
    return Fernet(_KEY_PATH.read_bytes())


def encrypt(s: str) -> str:
    return "enc:" + _fernet().encrypt(s.encode()).decode() if s else ""


def decrypt(s: str) -> str:
    if s and isinstance(s, str) and s.startswith("enc:"):
        try:
            return _fernet().decrypt(s[4:].encode()).decode()
        except Exception:
            return ""
    return s or ""

TERMINAL = ("done", "blocked", "error", "interrupted")


def _conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB_PATH, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA busy_timeout=5000")
    return c


def init_db() -> None:
    with _conn() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS sessions(
            sid TEXT PRIMARY KEY, token TEXT, login TEXT, avatar TEXT, created_at REAL);
        CREATE TABLE IF NOT EXISTS runs(
            id TEXT PRIMARY KEY, login TEXT, flow TEXT, mode TEXT, tracks TEXT,
            inputs TEXT, story TEXT, status TEXT, created_at REAL, updated_at REAL, result TEXT);
        CREATE TABLE IF NOT EXISTS run_events(
            run_id TEXT, seq INTEGER, type TEXT, payload TEXT, ts REAL,
            PRIMARY KEY(run_id, seq));
        CREATE INDEX IF NOT EXISTS idx_runs_login ON runs(login, created_at DESC);
        CREATE TABLE IF NOT EXISTS projects(
            id TEXT PRIMARY KEY, login TEXT, name TEXT, config TEXT, created_at REAL, updated_at REAL);
        CREATE INDEX IF NOT EXISTS idx_projects_login ON projects(login);
        """)


# ---------- projects (saved, reusable per-project config; secrets encrypted) ----------
def _redact(cfg: dict) -> dict:
    """A UI-safe copy: secret values removed, replaced by has_<field> booleans."""
    out = dict(cfg or {})
    for f in SECRET_FIELDS:
        out[f"has_{f}"] = bool(out.get(f))
        out.pop(f, None)
    return out


def save_project(login: str | None, name: str, config: dict, project_id: str | None = None) -> str:
    pid = project_id or uuid.uuid4().hex
    cfg = dict(config or {})
    # keep existing secret if the UI sent a blank (means "unchanged")
    existing = get_project(pid, reveal=True) if project_id else None
    for f in SECRET_FIELDS:
        if cfg.get(f):
            cfg[f] = encrypt(cfg[f])
        elif existing and existing.get(f):
            cfg[f] = existing[f] if str(existing[f]).startswith("enc:") else encrypt(existing[f])
        else:
            cfg[f] = ""
    now = time.time()
    with _write_lock, _conn() as c:
        row = c.execute("SELECT created_at FROM projects WHERE id=?", (pid,)).fetchone()
        created = row["created_at"] if row else now
        c.execute("INSERT OR REPLACE INTO projects(id,login,name,config,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                  (pid, login, name, json.dumps(cfg), created, now))
    return pid


def list_projects(login: str | None) -> list[dict]:
    with _conn() as c:
        if login:
            rows = c.execute("SELECT * FROM projects WHERE login=? OR login IS NULL ORDER BY updated_at DESC", (login,))
        else:
            rows = c.execute("SELECT * FROM projects ORDER BY updated_at DESC")
        return [{"id": r["id"], "name": r["name"], "updated_at": r["updated_at"],
                 "config": _redact(json.loads(r["config"] or "{}"))} for r in rows]


def get_project(project_id: str, reveal: bool = False) -> dict | None:
    with _conn() as c:
        r = c.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
    if not r:
        return None
    cfg = json.loads(r["config"] or "{}")
    if reveal:   # decrypt secrets for run use
        for f in SECRET_FIELDS:
            cfg[f] = decrypt(cfg.get(f, ""))
        return cfg
    d = _redact(cfg)
    d.update({"id": r["id"], "name": r["name"]})
    return d


def delete_project(project_id: str) -> None:
    with _write_lock, _conn() as c:
        c.execute("DELETE FROM projects WHERE id=?", (project_id,))


def mark_interrupted() -> int:
    """On startup, any run still 'running'/'queued' was orphaned by a restart."""
    with _write_lock, _conn() as c:
        cur = c.execute("UPDATE runs SET status='interrupted', updated_at=? "
                        "WHERE status IN ('running','queued')", (time.time(),))
        return cur.rowcount


# ---------- sessions ----------
def save_session(sid: str, token: str, login: str, avatar: str) -> None:
    with _write_lock, _conn() as c:
        c.execute("INSERT OR REPLACE INTO sessions(sid,token,login,avatar,created_at) VALUES(?,?,?,?,?)",
                  (sid, token, login, avatar, time.time()))


def delete_session(sid: str) -> None:
    with _write_lock, _conn() as c:
        c.execute("DELETE FROM sessions WHERE sid=?", (sid,))


def all_sessions() -> list[dict]:
    with _conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM sessions")]


# ---------- runs ----------
def create_run(run_id: str, login: str | None, flow: str, mode: str,
               tracks, inputs, story) -> None:
    now = time.time()
    with _write_lock, _conn() as c:
        c.execute("INSERT INTO runs(id,login,flow,mode,tracks,inputs,story,status,created_at,updated_at,result)"
                  " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                  (run_id, login, flow, mode, json.dumps(tracks or []), json.dumps(inputs or {}),
                   json.dumps(story or {}), "queued", now, now, None))


def set_status(run_id: str, status: str) -> None:
    with _write_lock, _conn() as c:
        c.execute("UPDATE runs SET status=?, updated_at=? WHERE id=?", (status, time.time(), run_id))


def finish_run(run_id: str, status: str, result: dict) -> None:
    with _write_lock, _conn() as c:
        c.execute("UPDATE runs SET status=?, result=?, updated_at=? WHERE id=?",
                  (status, json.dumps(result), time.time(), run_id))


def append_event(run_id: str, seq: int, type_: str, payload: dict) -> None:
    with _write_lock, _conn() as c:
        c.execute("INSERT OR REPLACE INTO run_events(run_id,seq,type,payload,ts) VALUES(?,?,?,?,?)",
                  (run_id, seq, type_, json.dumps(payload, default=str), time.time()))


def events_after(run_id: str, after_seq: int) -> list[dict]:
    with _conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT seq,type,payload FROM run_events WHERE run_id=? AND seq>? ORDER BY seq",
            (run_id, after_seq))]


def get_run(run_id: str) -> dict | None:
    with _conn() as c:
        row = c.execute("SELECT rowid AS num, * FROM runs WHERE id=?", (run_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        for k in ("tracks", "inputs", "story", "result"):
            d[k] = json.loads(d[k]) if d.get(k) else None
        return d


def run_status(run_id: str) -> str | None:
    with _conn() as c:
        row = c.execute("SELECT status FROM runs WHERE id=?", (run_id,)).fetchone()
        return row["status"] if row else None


def list_runs(login: str | None, limit: int = 50) -> list[dict]:
    """Recent runs for a user (or all when unauthenticated / PAT mode), including the
    configuration each run was started with (tracks + inputs)."""
    cols = "rowid AS num, id,login,flow,mode,tracks,inputs,status,created_at,updated_at"
    with _conn() as c:
        if login:
            rows = c.execute(f"SELECT {cols} FROM runs WHERE login=? OR login IS NULL "
                             "ORDER BY rowid DESC LIMIT ?", (login, limit))
        else:
            rows = c.execute(f"SELECT {cols} FROM runs ORDER BY rowid DESC LIMIT ?", (limit,))
        out = []
        for r in rows:
            d = dict(r)
            for k in ("tracks", "inputs"):
                try:
                    d[k] = json.loads(d[k]) if d.get(k) else None
                except Exception:
                    d[k] = None
            out.append(d)
        return out
