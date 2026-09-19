"""Jira Cloud integration — the live requirements source of truth.

Auth: a Jira Cloud site URL (https://<you>.atlassian.net) + your account email + an API token
(id.atlassian.com/manage-profile/security/api-tokens). Basic auth over the REST v3 API, stdlib
urllib only. Fetches an issue and extracts its acceptance criteria so the AC agent can work from
the actual ticket — not pasted text.
"""
import base64
import json
import urllib.error
import urllib.parse
import urllib.request


def _headers(email: str, token: str) -> dict:
    auth = base64.b64encode(f"{email}:{token}".encode()).decode()
    return {"Authorization": f"Basic {auth}", "Accept": "application/json",
            "Content-Type": "application/json", "User-Agent": "agentic-testing-pipeline"}


def _req(base: str, path: str, email: str, token: str, method: str = "GET", body: dict | None = None):
    url = base.rstrip("/") + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, method=method, data=data, headers=_headers(email, token))
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode(errors="ignore")[:300]
        except Exception:
            pass
        raise urllib.error.HTTPError(e.url, e.code, f"Jira {method} {path} → {e.reason}: {detail}", e.headers, None)


def _adf_to_text(node) -> str:
    """Flatten Atlassian Document Format (rich text JSON) to plain text."""
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    out = []
    t = node.get("type")
    if t == "text":
        out.append(node.get("text", ""))
    for child in node.get("content", []) or []:
        out.append(_adf_to_text(child))
    txt = "".join(out)
    if t in ("paragraph", "heading", "listItem", "codeBlock", "blockquote"):
        txt += "\n"
    if t in ("bulletList", "orderedList"):
        txt += "\n"
    return txt


def test_connection(base: str, email: str, token: str) -> dict:
    """Verify creds and return the current user + a few projects."""
    me = _req(base, "/rest/api/3/myself", email, token)
    projects = list_projects(base, email, token)
    return {"ok": True, "account": me.get("displayName") or me.get("emailAddress"),
            "projects": projects[:50]}


def list_projects(base: str, email: str, token: str) -> list[dict]:
    r = _req(base, "/rest/api/3/project/search?maxResults=50", email, token)
    return [{"key": p["key"], "name": p["name"]} for p in r.get("values", [])]


def list_issues(base: str, email: str, token: str, project_key: str, limit: int = 25) -> list[dict]:
    jql = urllib.parse.quote(f"project = {project_key} ORDER BY updated DESC")
    r = _req(base, f"/rest/api/3/search?jql={jql}&maxResults={limit}&fields=summary,issuetype,status,priority",
             email, token)
    return [{"key": i["key"], "summary": i["fields"].get("summary", ""),
             "type": (i["fields"].get("issuetype") or {}).get("name", ""),
             "status": (i["fields"].get("status") or {}).get("name", "")}
            for i in r.get("issues", [])]


def _adf_from_text(text: str) -> dict:
    """Build an Atlassian Document Format doc from plain text (one paragraph per line)."""
    paras = [{"type": "paragraph", "content": [{"type": "text", "text": ln}]}
             for ln in (text or "").split("\n") if ln.strip()]
    return {"type": "doc", "version": 1, "content": paras or [{"type": "paragraph", "content": []}]}


def add_comment(base: str, email: str, token: str, key: str, text: str) -> None:
    _req(base, f"/rest/api/3/issue/{key}/comment", email, token, "POST", {"body": _adf_from_text(text)})


def _subtask_type(base: str, email: str, token: str, project_key: str) -> str | None:
    try:
        meta = _req(base, f"/rest/api/3/issue/createmeta?projectKeys={project_key}&expand=projects.issuetypes",
                    email, token)
        for p in meta.get("projects", []):
            for it in p.get("issuetypes", []):
                if it.get("subtask"):
                    return it["name"]
    except Exception:
        pass
    return None


def create_test_cases(base: str, email: str, token: str, parent_key: str, cases: list[dict]) -> dict:
    """Store the derived functional test cases IN Jira, under the parent ticket — so Jira is the
    system of record. Creates a sub-task per case when possible; else posts a summary comment."""
    project_key = parent_key.split("-")[0]
    st = _subtask_type(base, email, token, project_key)
    created = []
    if st:
        for c in cases:
            steps = "\n".join(f"{i + 1}. {s}" for i, s in enumerate(c.get("steps", []) or []))
            desc = (f"Steps:\n{steps}\n\nExpected: {c.get('expected_result', '')}\n"
                    f"Covers AC: {', '.join(c.get('covers_ac', []) or []) or '—'}\n"
                    f"Priority: {c.get('priority', '—')} · Automatable: {c.get('automatable', True)}"
                    + (f"\nNote: {c.get('automation_note')}" if c.get("automation_note") else ""))
            try:
                r = _req(base, "/rest/api/3/issue", email, token, "POST", {"fields": {
                    "project": {"key": project_key}, "parent": {"key": parent_key},
                    "issuetype": {"name": st}, "summary": f"{c.get('id')} {c.get('title')}"[:250],
                    "description": _adf_from_text(desc)}})
                created.append(r["key"])
            except Exception:
                pass
        if created:
            return {"method": "subtasks", "created": created}
    # fallback: a single structured comment listing every case
    text = (f"Functional test cases derived from the acceptance criteria ({len(cases)}):\n\n"
            + "\n".join(f"{c.get('id')} · {c.get('title')} "
                        f"[{'automatable' if c.get('automatable', True) else 'manual: ' + (c.get('automation_note') or '')}]"
                        for c in cases))
    try:
        add_comment(base, email, token, parent_key, text)
        return {"method": "comment", "created": []}
    except Exception as exc:
        return {"method": "none", "error": str(exc)}


def fetch_issue(base: str, email: str, token: str, key: str) -> dict:
    """Fetch an issue and extract its acceptance criteria (from a dedicated AC section in the
    description, else the whole description). Returns {key, summary, description, ac_text}."""
    issue = _req(base, f"/rest/api/3/issue/{key}?fields=summary,description,issuetype,priority,labels",
                 email, token)
    f = issue.get("fields", {})
    summary = f.get("summary", "")
    desc = _adf_to_text(f.get("description")).strip()

    # If the description has an "Acceptance Criteria" heading, prefer that section.
    ac = desc
    low = desc.lower()
    for marker in ("acceptance criteria", "acceptance criterion", "ac:"):
        idx = low.find(marker)
        if idx != -1:
            ac = desc[idx:].strip()
            break

    return {"key": issue.get("key", key), "summary": summary, "description": desc,
            "ac_text": (f"{summary}\n\n{ac}").strip(),
            "type": (f.get("issuetype") or {}).get("name", ""),
            "labels": f.get("labels", [])}
