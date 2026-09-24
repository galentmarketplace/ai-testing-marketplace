"""Child-process isolation — the platform's secrets must never reach customer code.

Agents clone customer repositories and execute code from them (`npm install`, `npm test`, `go test`,
Playwright specs, k6 scripts, scanners). Before this module those children inherited the server's whole
environment, so one malicious `package.json` could read ANTHROPIC_API_KEY, GITHUB_CLIENT_SECRET,
JENKINS_TOKEN and the rest.

`child_env()` builds the environment from an ALLOW-LIST instead: the OS basics a toolchain needs, plus
exactly the variables the caller passes. Anything else — every credential the server holds — is dropped.

Also here: `run_workspace()`, the per-run directory for generated artifacts, so two runs never overwrite
each other's reports and an artifact link always resolves to the run that produced it.
"""
import os
import re
from pathlib import Path

from . import runctx
from .config import GENERATED_DIR

# Variables a build/test toolchain legitimately needs. Anything not listed here is not passed on.
_ALLOWED = (
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "TMPDIR", "TEMP", "TMP",
    "LANG", "LC_ALL", "LC_CTYPE", "TZ", "TERM",
    "SystemRoot", "COMSPEC", "PATHEXT", "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE",
    # toolchain locations (paths, not credentials)
    "NODE_PATH", "NPM_CONFIG_CACHE", "npm_config_cache", "PLAYWRIGHT_BROWSERS_PATH",
    "GOPATH", "GOCACHE", "GOMODCACHE", "GOROOT", "JAVA_HOME", "PYENV_ROOT",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "NODE_EXTRA_CA_CERTS",
)

# Belt and braces: never forward anything that looks like a credential, even if it were allow-listed.
_SECRETISH = re.compile(r"(?i)(key|token|secret|password|passwd|credential|cookie|session|auth)")


def child_env(extra: dict | None = None, *, inherit: tuple = ()) -> dict:
    """A minimal environment for a child process: OS/toolchain basics + `extra` (explicit, per call).

    `inherit` may name additional variables to copy from the server's environment — use it only for
    non-secret configuration; secret-looking names are refused.
    """
    env = {k: os.environ[k] for k in _ALLOWED if k in os.environ}
    for k in inherit:
        if k in os.environ and not _SECRETISH.search(k):
            env[k] = os.environ[k]
    env["CI"] = "1"
    for k, v in (extra or {}).items():
        if v is None:
            continue
        env[str(k)] = str(v)
    return env


def run_workspace(subdir: str = "") -> Path:
    """`generated/runs/<run_id>/<subdir>` for the active run, else the legacy shared `generated/<subdir>`.

    Keeps every run's artifacts separate (so links never point at a later run's file) while the CLI and
    tests, which have no run context, behave exactly as before.
    """
    rid = (runctx.current().run_id if runctx.current() else None)
    base = (GENERATED_DIR / "runs" / re.sub(r"[^A-Za-z0-9_-]", "", rid)) if rid else GENERATED_DIR
    out = base / subdir if subdir else base
    out.mkdir(parents=True, exist_ok=True)
    return out


def run_id_for_path(path: Path | str) -> str | None:
    """The run that owns an artifact path, or None for a legacy shared-directory artifact."""
    try:
        rel = Path(path).resolve().relative_to((GENERATED_DIR / "runs").resolve())
    except Exception:
        return None
    return rel.parts[0] if rel.parts else None
