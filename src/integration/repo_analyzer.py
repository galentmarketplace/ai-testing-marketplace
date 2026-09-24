"""Repo analyzer — turns a real target repository into a structured surface the
agents test against. Fully DETERMINISTIC (no LLM): it clones the repo and parses
the code, so it works with or without an API key.

Currently understands the IDURAR / MERN convention (Express `appModels` → CRUD
API, React `router/routes.jsx` → UI routes). The `stack` field lets agents adapt;
new stacks are added by extending `_discover_*`.
"""
import re
import subprocess
from glob import glob
from pathlib import Path

from .. import sandbox
from ..config import PROJECT_ROOT

REPOS_DIR = PROJECT_ROOT / "repos"

# IDURAR's appApi.js generates these routes for every entity (mirrored exactly).
_CRUD = [("POST", "create"), ("GET", "read/:id"), ("PATCH", "update/:id"),
         ("DELETE", "delete/:id"), ("GET", "search"), ("GET", "list"),
         ("GET", "listAll"), ("GET", "filter"), ("GET", "summary")]


def _ensure_local(source: str) -> Path:
    """Accept a local path or a git URL. Clones URLs (shallow) into repos/<name>."""
    if source and (source.startswith("http") or source.startswith("git@")):
        slug = re.sub(r"[^a-zA-Z0-9_.-]", "-", source.rstrip("/").split("/")[-1].replace(".git", ""))
        dest = REPOS_DIR / slug
        if not dest.exists():
            REPOS_DIR.mkdir(parents=True, exist_ok=True)
            subprocess.run(["git", "clone", "--depth", "1", source, str(dest)],
                           check=True, capture_output=True, timeout=300, env=sandbox.child_env())
        return dest
    return Path(source)


def _detect_stack(root: Path) -> str:
    be = (root / "backend" / "package.json").exists()
    fe = (root / "frontend" / "package.json").exists()
    if be and fe:
        return "MERN (Express API + React/Vite)"
    if (root / "package.json").exists():
        return "Node/JS project"
    return "unknown"


def _discover_api(root: Path) -> list[dict]:
    """Real REST surface: each appModel entity → its generated CRUD routes."""
    api = []
    for f in sorted(glob(str(root / "backend/src/models/appModels/**/*.js"), recursive=True)):
        entity = Path(f).stem.lower()
        routes = [{"method": m, "path": f"/api/{entity}/{p}"} for m, p in _CRUD]
        if entity in ("invoice", "quote", "payment"):
            routes.append({"method": "POST", "path": f"/api/{entity}/mail"})
        if entity == "quote":
            routes.append({"method": "GET", "path": f"/api/{entity}/convert/:id"})
        api.append({"entity": entity, "routes": routes})
    return api


_FRONTEND_DIRS = ["frontend/src", "src", "app", "pages", "client/src", "web/src", "ui/src"]


def _frontend_root(root: Path) -> Path:
    for d in _FRONTEND_DIRS:
        if (root / d).exists():
            return root / d
    return root


def _discover_ui(root: Path) -> dict:
    """Derive UI routes + the LOGIN path + auth behaviour from the app's code (any React/Vue/
    Angular/Next router) — the source of truth — instead of assuming '/login'.

    Resolves route-path CONSTANTS too (e.g. `<Route path={ROUTES.LOGIN}>` where
    `ROUTES = { LOGIN: '/' }` in a constants file)."""
    fr = _frontend_root(root)
    files = [Path(f) for f in glob(str(fr / "**/*.jsx"), recursive=True)
             + glob(str(fr / "**/*.tsx"), recursive=True)
             + glob(str(fr / "**/*.js"), recursive=True)
             + glob(str(fr / "**/*.ts"), recursive=True)
             + glob(str(fr / "**/*.vue"), recursive=True)][:500]

    consts: dict[str, str] = {}      # "ROUTES.LOGIN" -> "/",  "LOGIN_PATH" -> "/login"
    texts = {}
    for f in files:
        if any(s in str(f) for s in ("__tests__", ".test.", ".spec.", ".stories.", "/node_modules/")):
            continue
        try:
            t = f.read_text(errors="ignore")
        except Exception:
            continue
        texts[f] = t
        for objname, body in re.findall(r"(?:export\s+)?const\s+([A-Z][A-Za-z0-9_]*)\s*=\s*\{([^}]*)\}", t):
            for k, v in re.findall(r"""([A-Za-z0-9_]+)\s*:\s*['"]([^'"]+)['"]""", body):
                consts[f"{objname}.{k}"] = v
        for k, v in re.findall(r"""const\s+([A-Za-z0-9_]+)\s*=\s*['"](/[^'"]*)['"]""", t):
            consts[k] = v

    routes, login_route, guarded = set(), None, False
    for _f, t in texts.items():
        for p in re.findall(r"""path[=:]\s*['"]([^'"]+)['"]""", t):          # literal paths
            if p and p != "*":
                routes.add(p)
        for ref in re.findall(r"path[=:]\s*\{?\s*([A-Z][A-Za-z0-9_.]+)\s*\}?", t):  # constant-ref paths
            if ref in consts:
                routes.add(consts[ref])
        # login route: a <Route> whose element/component name looks like Login/SignIn
        for ref, comp in re.findall(r"path[=:]\s*\{?\s*([A-Za-z0-9_.]+)\s*\}?[^>]{0,80}?element=\{?\s*<?\s*([A-Za-z0-9_]+)", t):
            if re.search(r"log\s?in|sign\s?in|sign-in|auth", comp, re.I):
                login_route = consts.get(ref, ref if ref.startswith("/") else login_route)
        for ref in re.findall(r"path[=:]\s*\{?\s*([A-Za-z0-9_.]+)\s*\}?", t):   # or a login-named constant
            if re.search(r"login|signin", ref, re.I) and ref in consts:
                login_route = consts[ref]
        if re.search(r"PrivateRoute|ProtectedRoute|RequireAuth|Navigate\s+to=\{?\s*[A-Za-z0-9_.]*LOGIN", t):
            guarded = True
    if not login_route:   # fallback: a route path literally containing login/signin
        login_route = next((r for r in sorted(routes) if re.search(r"login|signin|sign-in", r, re.I)), None)

    return {"routes": sorted(routes)[:40], "login_route": login_route, "auth_guarded": guarded}


def _discover_models(root: Path) -> list[dict]:
    """Best-effort field extraction from each mongoose appModel (for contracts)."""
    models = []
    for f in sorted(glob(str(root / "backend/src/models/appModels/**/*.js"), recursive=True)):
        text = Path(f).read_text(errors="ignore")
        fields = re.findall(r"^\s{2,}([a-zA-Z_][a-zA-Z0-9_]*):\s*\{", text, re.M)
        models.append({"name": Path(f).stem, "fields": list(dict.fromkeys(fields))[:25]})
    return models


def analyze_repo(source: str) -> dict:
    """Clone/locate the repo and return its testable surface. Never raises — on any
    failure it returns a structured error so the pipeline can proceed gracefully."""
    try:
        root = _ensure_local(source)
        if not root.exists():
            return {"source": source, "ok": False, "error": f"path not found: {root}"}
        api = _discover_api(root)
        ui = _discover_ui(root)
        models = _discover_models(root)
        return {
            "source": source, "ok": True, "path": str(root),
            "stack": _detect_stack(root),
            "entities": [a["entity"] for a in api],
            "endpoint_count": sum(len(a["routes"]) for a in api),
            "api": api, "ui_routes": ui["routes"], "models": models,
            "login_route": ui.get("login_route"),      # derived from the code (source of truth), not assumed
            "auth_guarded": ui.get("auth_guarded"),
        }
    except subprocess.CalledProcessError as exc:
        return {"source": source, "ok": False, "error": f"git clone failed: {exc.stderr.decode()[:200] if exc.stderr else exc}"}
    except Exception as exc:  # analysis is best-effort; never wedge the run
        return {"source": source, "ok": False, "error": str(exc)}
