"""Per-run working copy of the application repository.

The Dev Agent edits real source files. Those edits must NOT land in `repos/<name>`, which is
the analyzer's shared clone cache: one run's half-finished feature would then leak into every
later run's analysis, its deploy, and its tests. Two concurrent runs on the same repo would
also overwrite each other.

So each run gets its own working tree. A git worktree is used where possible because it shares
the object store (cheap, no re-download), falling back to a plain copy otherwise. `node_modules`
is symlinked from the cache when present, so the per-run copy does not pay a full dependency
install just to exist.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from .. import sandbox

_WORKTREES: dict[str, list[tuple[Path, Path]]] = {}   # run_id -> [(source_repo, worktree)]

_SKIP = {".git", "node_modules", "dist", "build", ".next", "__pycache__", ".venv", ".pytest_cache"}


def _is_git_repo(p: Path) -> bool:
    return (p / ".git").exists()


def _git(args: list[str], cwd: Path, timeout: int = 180) -> tuple[int, str]:
    p = subprocess.run(["git", *args], cwd=str(cwd), env=sandbox.child_env(),
                       capture_output=True, text=True, timeout=timeout)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def app_workdir(repo_path: str | Path, run_id: str = "", *, reuse: bool = True) -> dict:
    """Materialise a writable per-run copy of `repo_path`.

    Returns {ok, path, mode, error?}. `mode` is "worktree" | "copy" | "reused".
    """
    src = Path(repo_path).resolve()
    if not src.is_dir():
        return {"ok": False, "error": f"repo path not found: {src}"}

    dest = sandbox.run_workspace("app") / src.name
    if reuse and dest.is_dir() and any(dest.iterdir()):
        return {"ok": True, "path": str(dest), "mode": "reused"}

    dest.parent.mkdir(parents=True, exist_ok=True)
    mode = "copy"

    if _is_git_repo(src):
        # --detach so the worktree pins the current commit without claiming a branch name.
        rc, out = _git(["worktree", "add", "--detach", "--force", str(dest), "HEAD"], src)
        if rc == 0:
            mode = "worktree"
            _WORKTREES.setdefault(run_id or "local", []).append((src, dest))
        else:
            shutil.rmtree(dest, ignore_errors=True)

    if mode == "copy":
        try:
            shutil.copytree(src, dest,
                            ignore=shutil.ignore_patterns(*_SKIP), dirs_exist_ok=True)
        except Exception as exc:
            return {"ok": False, "error": f"could not copy the repo: {exc}"}

    # Reuse the cache's installed dependencies so the copy does not re-install from scratch.
    cached_modules = src / "node_modules"
    link = dest / "node_modules"
    if cached_modules.is_dir() and not link.exists():
        try:
            link.symlink_to(cached_modules, target_is_directory=True)
        except Exception:
            pass                                  # a full install will just run instead

    return {"ok": True, "path": str(dest), "mode": mode}


def changed_files(workdir: str | Path) -> list[str]:
    """Repo-relative paths the agent actually changed, for the commit and the PR body."""
    wd = Path(workdir)
    if not _is_git_repo(wd) and not (wd / ".git").exists():
        return []
    rc, out = _git(["status", "--porcelain", "--untracked-files=all"], wd)
    if rc != 0:
        return []
    paths = []
    for line in out.splitlines():
        if len(line) > 3:
            p = line[3:].strip().strip('"')
            if not any(part in _SKIP for part in Path(p).parts):
                paths.append(p)
    return sorted(paths)


def cleanup_run(run_id: str = "") -> int:
    """Detach every worktree this run created. Leaving them registered would make the shared
    clone accumulate stale worktree entries run after run."""
    entries = _WORKTREES.pop(run_id or "local", [])
    for src, dest in entries:
        try:
            _git(["worktree", "remove", "--force", str(dest)], src, timeout=60)
        except Exception:
            pass
    return len(entries)
