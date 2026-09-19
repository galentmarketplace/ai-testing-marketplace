"""GitHub integration — connect with a token, browse/create repos, commit files, open PRs.

Auth: a Personal Access Token in GITHUB_TOKEN (classic with `repo` scope, or a
fine-grained token with Contents + Pull requests + Administration write). All calls
go through the REST API via stdlib urllib (no extra deps). Every function raises on
HTTP error; callers decide how to surface it.
"""
import base64
import contextvars
import json
import os
import urllib.error
import urllib.request

API = "https://api.github.com"

# The active user's OAuth token for this request/run. Falls back to a single-user
# PAT in GITHUB_TOKEN when OAuth isn't configured.
_TOKEN: contextvars.ContextVar = contextvars.ContextVar("gh_token", default=None)


def set_token(token: str | None) -> None:
    _TOKEN.set(token or None)


def _token() -> str:
    return _TOKEN.get() or os.environ.get("GITHUB_TOKEN", "")


def connected() -> bool:
    return bool(_token())


def _req(method: str, path: str, body: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        API + path, method=method, data=data,
        headers={
            "Authorization": f"Bearer {_token()}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "agentic-testing-pipeline",
            "Content-Type": "application/json",
        })
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode(errors="ignore")[:300]
        except Exception:
            pass
        raise urllib.error.HTTPError(e.url, e.code, f"{method} {path} → {e.reason}: {detail}",
                                     e.headers, None)


# ---------- OAuth (per-user login) ----------
def oauth_configured() -> bool:
    return bool(os.environ.get("GITHUB_CLIENT_ID") and os.environ.get("GITHUB_CLIENT_SECRET"))


def oauth_authorize_url(redirect_uri: str, state: str, scope: str = "repo") -> str:
    from urllib.parse import urlencode
    return "https://github.com/login/oauth/authorize?" + urlencode({
        "client_id": os.environ.get("GITHUB_CLIENT_ID", ""),
        "redirect_uri": redirect_uri, "scope": scope, "state": state,
    })


def oauth_exchange(code: str, redirect_uri: str) -> str:
    """Exchange an OAuth code for a user access token."""
    from urllib.parse import urlencode
    body = urlencode({
        "client_id": os.environ.get("GITHUB_CLIENT_ID", ""),
        "client_secret": os.environ.get("GITHUB_CLIENT_SECRET", ""),
        "code": code, "redirect_uri": redirect_uri,
    }).encode()
    req = urllib.request.Request(
        "https://github.com/login/oauth/access_token", data=body,
        headers={"Accept": "application/json", "User-Agent": "agentic-testing-pipeline"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read()).get("access_token", "")


# ---------- identity & discovery ----------
def whoami() -> dict:
    u = _req("GET", "/user")
    return {"login": u.get("login"), "name": u.get("name"), "avatar": u.get("avatar_url")}


def list_repos(limit: int = 100) -> list[dict]:
    repos = _req("GET", f"/user/repos?per_page={limit}&sort=updated&affiliation=owner,collaborator")
    return [{"full_name": r["full_name"], "private": r["private"],
             "default_branch": r.get("default_branch", "main")} for r in repos]


def repo_info(full_name: str) -> dict:
    return _req("GET", f"/repos/{full_name}")


# ---------- create ----------
def create_repo(name: str, private: bool = True, description: str = "") -> dict:
    """Create the repo, or reuse it if it already exists (idempotent)."""
    try:
        r = _req("POST", "/user/repos",
                 {"name": name, "private": private, "description": description, "auto_init": True})
    except urllib.error.HTTPError as e:
        if e.code == 422:  # name already exists on this account → reuse it
            r = _req("GET", f"/repos/{whoami()['login']}/{name}")
        else:
            raise
    return {"full_name": r["full_name"], "default_branch": r.get("default_branch", "main"),
            "html_url": r["html_url"]}


# ---------- commit + PR ----------
def _branch_sha(full_name: str, branch: str) -> str:
    return _req("GET", f"/repos/{full_name}/git/ref/heads/{branch}")["object"]["sha"]


def ensure_branch(full_name: str, branch: str, from_branch: str) -> None:
    try:
        _req("GET", f"/repos/{full_name}/git/ref/heads/{branch}")  # already exists
    except urllib.error.HTTPError:
        _req("POST", f"/repos/{full_name}/git/refs",
             {"ref": f"refs/heads/{branch}", "sha": _branch_sha(full_name, from_branch)})


def put_file(full_name: str, path: str, content: str, branch: str, message: str) -> None:
    body = {"message": message, "branch": branch,
            "content": base64.b64encode(content.encode()).decode()}
    try:  # update needs the existing blob sha
        existing = _req("GET", f"/repos/{full_name}/contents/{path}?ref={branch}")
        if isinstance(existing, dict) and existing.get("sha"):
            body["sha"] = existing["sha"]
    except urllib.error.HTTPError:
        pass
    _req("PUT", f"/repos/{full_name}/contents/{path}", body)


def get_file(full_name: str, path: str, ref: str) -> str:
    """Fetch a file's text content from a branch/ref."""
    import urllib.parse
    r = _req("GET", f"/repos/{full_name}/contents/{urllib.parse.quote(path)}?ref={ref}")
    return base64.b64decode(r["content"]).decode(errors="ignore")


def compare_ahead(full_name: str, base: str, head: str) -> int:
    """How many commits `head` is ahead of `base` (0 => nothing to PR)."""
    try:
        return _req("GET", f"/repos/{full_name}/compare/{base}...{head}").get("ahead_by", 0)
    except Exception:
        return 1   # if we can't tell, assume there's something to PR


def open_pr(full_name: str, head: str, base: str, title: str, body: str) -> dict:
    pr = _req("POST", f"/repos/{full_name}/pulls",
              {"title": title, "head": head, "base": base, "body": body})
    return {"url": pr["html_url"], "number": pr["number"]}


def commit_files(full_name: str, files: dict[str, str], branch: str,
                 base: str | None = None, message: str = "commit") -> dict:
    """Commit {path: content} onto a branch (creating it) WITHOUT opening a PR.
    Returns {branch, base, committed, skipped}."""
    base = base or repo_info(full_name).get("default_branch", "main")
    ensure_branch(full_name, branch, base)
    committed, skipped = [], []
    for path, content in files.items():
        try:  # resilient: a single blocked file (e.g. needs workflow scope) shouldn't abort the push
            put_file(full_name, path, content, branch, f"{message}: {path}")
            committed.append(path)
        except urllib.error.HTTPError as e:
            skipped.append(f"{path} ({e.code})")
    if not committed:
        raise RuntimeError("no files could be committed (check token scopes)")
    return {"branch": branch, "base": base, "committed": len(committed), "skipped": skipped}


def commit_files_and_pr(full_name: str, files: dict[str, str], branch: str,
                        title: str, body: str, base: str | None = None) -> dict:
    """Commit {path: content} onto a new branch and open a PR. Returns {url, number, branch}."""
    r = commit_files(full_name, files, branch, base, message=title)
    pr = open_pr(full_name, branch, r["base"], title, body)
    return {**pr, **r}


# ---------- existing-framework scan ----------
_TEST_HINTS = {
    "@playwright/test": "playwright", "cypress": "cypress", "webdriverio": "webdriverio",
    "@wdio/cli": "webdriverio", "selenium-webdriver": "selenium", "jest": "jest",
    "mocha": "mocha", "nightwatch": "nightwatch", "puppeteer": "puppeteer",
}


def scan_framework(full_name: str) -> dict:
    """Detect the repo's existing test framework and reusable structure, so the agent
    can write new cases on top of what's there instead of reinventing it."""
    tree, pkg = [], {}
    try:
        info = repo_info(full_name)
        branch = info.get("default_branch", "main")
        sha = _req("GET", f"/repos/{full_name}/git/ref/heads/{branch}")["object"]["sha"]
        tree = _req("GET", f"/repos/{full_name}/git/trees/{sha}?recursive=1").get("tree", [])
        try:
            raw = _req("GET", f"/repos/{full_name}/contents/package.json?ref={branch}")
            pkg = json.loads(base64.b64decode(raw["content"]).decode())
        except Exception:
            pkg = {}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}

    deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
    frameworks = sorted({v for k, v in _TEST_HINTS.items() if k in deps})
    paths = [t["path"] for t in tree if t.get("type") == "blob"]
    spec_files = [p for p in paths if any(s in p for s in (".spec.", ".test.", ".cy.", "_test."))]
    page_objects = [p for p in paths if "page" in p.lower() and p.endswith((".ts", ".js"))]
    helpers = [p for p in paths if any(h in p.lower() for h in ("fixture", "helper", "util", "support"))
               and p.endswith((".ts", ".js"))]
    configs = [p for p in paths if any(c in p for c in ("playwright.config", "cypress.config",
               "wdio.conf", "jest.config", "nightwatch.conf"))]
    return {
        "ok": True, "has_framework": bool(frameworks),
        "frameworks": frameworks, "configs": configs,
        "spec_files": spec_files[:40], "page_objects": page_objects[:40],
        "helpers": helpers[:40], "scripts": pkg.get("scripts", {}),
        "file_count": len(paths),
    }
