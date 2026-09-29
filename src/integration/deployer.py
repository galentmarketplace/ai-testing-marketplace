"""Deploy the app under test to an ephemeral environment and boot it.

This closes the gap between "an agent wrote a feature" and "the automation can drive that
feature": the generated code is only testable once it is actually running somewhere. The
deployer builds the application, serves it on a free loopback port, waits until it really
answers, and hands back a URL that the Playwright agent and the runner target instead of
whatever static `base_url` the project config carried.

Strategies (auto-detected, overridable with `deploy_strategy`):
  node    — npm ci → npm run build → serve (vite preview / npm start) on a free port
  docker  — docker build → docker run -p <free>:<container port>
  static  — serve an already-built directory over HTTP
  none    — skip; the run falls back to the configured base_url

Every handle is tracked per run so `teardown_run()` can guarantee nothing is left listening
after the run finishes. Child processes get the sandboxed allow-listed environment, so a
deployed app can never read platform credentials.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

from .. import sandbox

# run_id -> [handle, ...]; a handle is {"kind": "proc"|"docker", ...}
_HANDLES: dict[str, list[dict]] = {}

BUILD_TIMEOUT = int(os.environ.get("ATM_DEPLOY_BUILD_TIMEOUT", "900"))
BOOT_TIMEOUT = int(os.environ.get("ATM_DEPLOY_BOOT_TIMEOUT", "180"))


# --------------------------------------------------------------------------- utilities
def free_port() -> int:
    """Ask the OS for an unused loopback port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _have(tool: str) -> bool:
    return shutil.which(tool) is not None


def _pkg_scripts(repo: Path) -> dict:
    pj = repo / "package.json"
    if not pj.is_file():
        return {}
    try:
        return json.loads(pj.read_text()).get("scripts", {}) or {}
    except Exception:
        return {}


def detect_strategy(repo: Path) -> str:
    """Pick how to run this app.

    Node is preferred over Docker when both exist: many repos ship a CI/builder Dockerfile
    (test harness, toolchain image) rather than a serving one, and building it is far slower
    than a local build. An explicit `deploy_strategy` always wins over this guess.
    """
    scripts = _pkg_scripts(repo)
    if scripts.get("start") or (scripts.get("build") and scripts.get("preview")):
        if _have("npm"):
            return "node"
    if (repo / "Dockerfile").is_file() and _have("docker"):
        return "docker"
    for d in ("dist", "build", "public"):
        if (repo / d).is_dir():
            return "static"
    return "none"


def wait_ready(url: str, timeout: int = BOOT_TIMEOUT, proc: subprocess.Popen | None = None) -> bool:
    """Poll until the app actually answers. Any HTTP response counts: a 401/404 still proves
    something is listening and serving, which is what the automation needs."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc is not None and proc.poll() is not None:
            return False                      # the server died while we waited
        try:
            with urllib.request.urlopen(url, timeout=4) as r:
                if r.status:
                    return True
        except urllib.error.HTTPError:
            return True                       # responded, just not 2xx
        except Exception:
            pass
        time.sleep(0.6)
    return False


def _run(cmd: list[str], cwd: Path, log, timeout: int) -> tuple[int, str]:
    """Run a build step, tee-ing output into the run's log file."""
    log.write(f"\n$ {' '.join(cmd)}\n")
    log.flush()
    try:
        p = subprocess.run(cmd, cwd=str(cwd), env=sandbox.child_env(), text=True,
                           capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        log.write(f"[timeout after {timeout}s]\n")
        return 124, f"`{' '.join(cmd[:2])}` timed out after {timeout}s"
    out = (p.stdout or "") + (p.stderr or "")
    log.write(out)
    log.flush()
    return p.returncode, out[-1200:]


# --------------------------------------------------------------------------- strategies
def _deploy_node(repo: Path, port: int, log, extra_env: dict) -> dict:
    scripts = _pkg_scripts(repo)
    install = ["npm", "ci"] if (repo / "package-lock.json").is_file() else ["npm", "install"]
    rc, tail = _run(install, repo, log, BUILD_TIMEOUT)
    if rc != 0:
        return {"ok": False, "error": f"dependency install failed: {tail[-400:]}"}

    if scripts.get("build"):
        rc, tail = _run(["npm", "run", "build"], repo, log, BUILD_TIMEOUT)
        if rc != 0:
            return {"ok": False, "error": f"build failed: {tail[-400:]}"}

    if scripts.get("preview"):
        cmd = ["npm", "run", "preview", "--", "--port", str(port),
               "--strictPort", "--host", "127.0.0.1"]
    elif scripts.get("start"):
        cmd = ["npm", "start"]
    else:
        return {"ok": False, "error": "no `preview` or `start` script to serve the app"}

    env = sandbox.child_env({"PORT": str(port), "HOST": "127.0.0.1", **extra_env})
    log.write(f"\n$ {' '.join(cmd)}\n")
    log.flush()
    proc = subprocess.Popen(cmd, cwd=str(repo), env=env, stdout=log, stderr=log,
                            text=True, start_new_session=True)
    return {"ok": True, "handle": {"kind": "proc", "proc": proc, "port": port}, "port": port}


def _container_port(repo: Path) -> int:
    """First EXPOSE in the Dockerfile, else 80."""
    try:
        for ln in (repo / "Dockerfile").read_text().splitlines():
            s = ln.strip()
            if s.upper().startswith("EXPOSE"):
                parts = s.split()
                if len(parts) > 1:
                    return int(parts[1].split("/")[0])
    except Exception:
        pass
    return 80


def _deploy_docker(repo: Path, port: int, log, extra_env: dict, tag: str) -> dict:
    rc, tail = _run(["docker", "build", "-t", tag, "."], repo, log, BUILD_TIMEOUT)
    if rc != 0:
        return {"ok": False, "error": f"image build failed: {tail[-400:]}"}
    cport = _container_port(repo)
    name = tag.replace("/", "-").replace(":", "-")
    envs: list[str] = []
    for k, v in (extra_env or {}).items():
        envs += ["-e", f"{k}={v}"]
    cmd = (["docker", "run", "-d", "--rm", "--name", name,
            "-p", f"127.0.0.1:{port}:{cport}"] + envs + [tag])
    rc, out = _run(cmd, repo, log, 120)
    if rc != 0:
        return {"ok": False, "error": f"container start failed: {out[-400:]}"}
    return {"ok": True, "handle": {"kind": "docker", "name": name, "port": port}, "port": port}


def _deploy_static(repo: Path, port: int, log, _extra_env: dict) -> dict:
    root = next((repo / d for d in ("dist", "build", "public") if (repo / d).is_dir()), None)
    if root is None:
        return {"ok": False, "error": "no built directory (dist/build/public) to serve"}
    cmd = ["python3", "-m", "http.server", str(port), "--bind", "127.0.0.1",
           "--directory", str(root)]
    log.write(f"\n$ {' '.join(cmd)}\n")
    log.flush()
    proc = subprocess.Popen(cmd, cwd=str(repo), env=sandbox.child_env(), stdout=log,
                            stderr=log, text=True, start_new_session=True)
    return {"ok": True, "handle": {"kind": "proc", "proc": proc, "port": port}, "port": port}


# --------------------------------------------------------------------------- entry point
def deploy(repo_path: str | Path, *, run_id: str = "", strategy: str = "auto",
           port: int | None = None, env: dict | None = None,
           boot_timeout: int = BOOT_TIMEOUT, health_path: str = "/") -> dict:
    """Build and boot the app. Returns {ok, url, strategy, port, log, error?}."""
    repo = Path(repo_path)
    if not repo.is_dir():
        return {"ok": False, "strategy": "none", "error": f"repo path not found: {repo}"}

    chosen = strategy if strategy and strategy != "auto" else detect_strategy(repo)
    if chosen == "none":
        return {"ok": False, "strategy": "none",
                "error": "no way to serve this app (no npm start/preview, no Dockerfile, no built dir)"}

    port = port or free_port()
    log_dir = sandbox.run_workspace("deploy")
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "deploy.log"

    with open(log_path, "w") as log:
        log.write(f"deploy {repo}  strategy={chosen}  port={port}\n")
        if chosen == "node":
            r = _deploy_node(repo, port, log, env or {})
        elif chosen == "docker":
            r = _deploy_docker(repo, port, log, env or {}, tag=f"atm-aut:{(run_id or 'local')[:12]}")
        else:
            r = _deploy_static(repo, port, log, env or {})

    if not r.get("ok"):
        return {"ok": False, "strategy": chosen, "log": str(log_path), "error": r.get("error")}

    handle = r["handle"]
    _HANDLES.setdefault(run_id or "local", []).append(handle)

    url = f"http://127.0.0.1:{port}"
    proc = handle.get("proc") if handle["kind"] == "proc" else None
    if not wait_ready(url.rstrip("/") + health_path, boot_timeout, proc=proc):
        tail = ""
        try:
            tail = Path(log_path).read_text()[-600:]
        except Exception:
            pass
        teardown(handle)
        _HANDLES.get(run_id or "local", []).remove(handle) if handle in _HANDLES.get(run_id or "local", []) else None
        return {"ok": False, "strategy": chosen, "log": str(log_path),
                "error": f"app did not answer on {url} within {boot_timeout}s. {tail[-300:]}"}

    return {"ok": True, "url": url, "strategy": chosen, "port": port, "log": str(log_path)}


# --------------------------------------------------------------------------- teardown
def teardown(handle: dict) -> None:
    """Stop one deployment. Best effort — never raises."""
    try:
        if handle["kind"] == "docker":
            subprocess.run(["docker", "rm", "-f", handle["name"]], capture_output=True, timeout=60)
            return
        proc = handle.get("proc")
        if proc is None or proc.poll() is not None:
            return
        try:                                   # kill the whole process group (npm spawns children)
            os.killpg(os.getpgid(proc.pid), 15)
        except Exception:
            proc.terminate()
        try:
            proc.wait(timeout=12)
        except Exception:
            try:
                os.killpg(os.getpgid(proc.pid), 9)
            except Exception:
                proc.kill()
    except Exception:
        pass


def teardown_run(run_id: str) -> int:
    """Stop every deployment this run started. Called from the run's finally block, so a
    crashed or cancelled run never leaves a port listening."""
    handles = _HANDLES.pop(run_id or "local", [])
    for h in handles:
        teardown(h)
    return len(handles)


def active(run_id: str = "") -> list[dict]:
    hs = _HANDLES.get(run_id or "local", [])
    return [{"kind": h["kind"], "port": h.get("port")} for h in hs]
