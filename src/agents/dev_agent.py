"""Dev Agent — implements the feature from the acceptance criteria, in the real repository.

This agent used to be a skeleton: it wrote standalone files into a scratch folder, which
demonstrated the pipeline but produced nothing deployable. It now edits the actual application
source, so the Deploy step can build the change and the automation can drive it.

Grounding, in the same spirit as the Playwright agent:
  * it is given the repo's real stack, routes and a map of its source files
  * it is given the CURRENT contents of the files it is most likely to touch
  * it must return repo-relative paths, and edits are confined to the working copy

Isolation: edits land in a per-run worktree (see integration/workspace.py), never in the
analyzer's shared clone, so one run can never contaminate another's source.

Self-correction: on a retry after a failed UNIT gate, the previous failures are fed back in.
"""
from pathlib import Path

from ..integration import workspace
from ..integration.repo_analyzer import _ensure_local
from ..llm import call_llm_json
from ..state import PipelineState

SYSTEM = """You are a senior engineer implementing a feature in an EXISTING repository.

You are given the repository's stack, its file map, and the current contents of the files most
relevant to the change. Implement the acceptance criteria by editing that codebase.

Rules:
- `path` MUST be repo-relative (e.g. "src/pages/Cart.jsx"). Never absolute, never "..".
- To change an existing file, return its COMPLETE new contents, not a diff or a fragment.
- Match the conventions already present in the repo: same framework, same import style,
  same file layout. Do not introduce a new framework or a new directory scheme.
- Add or update a unit test for the behaviour when the repo already has a test setup.
- Change as little as possible. Do not reformat files you are not modifying.

Respond with ONLY a JSON object:
{"summary": "one line on what you changed",
 "files": [{"path": "<repo-relative>", "description": "...", "content": "<full file>"}]}"""

# Extensions worth showing the model, and the noise to keep out of the file map.
_CODE = {".js", ".jsx", ".ts", ".tsx", ".vue", ".svelte", ".py", ".go", ".java", ".rb", ".php"}
_SKIP_DIRS = {".git", "node_modules", "dist", "build", ".next", "__pycache__", ".venv",
              "coverage", "test-results", ".pytest_cache", "vendor"}
MAX_MAP = 220           # files listed in the map
MAX_SHOWN = 8           # files whose full contents are included
MAX_BYTES = 12_000      # per shown file


def _repo_source(state: PipelineState) -> str | None:
    inp = state.get("story", {}).get("inputs", {}) or {}
    return inp.get("repo") or inp.get("source_repo") or None


def _file_map(root: Path) -> list[str]:
    out = []
    for p in sorted(root.rglob("*")):
        if not p.is_file() or p.suffix not in _CODE:
            continue
        rel = p.relative_to(root)
        if any(part in _SKIP_DIRS for part in rel.parts):
            continue
        out.append(str(rel))
        if len(out) >= MAX_MAP:
            break
    return out


def _relevant(root: Path, files: list[str], ac_text: str) -> list[str]:
    """Rank files by how many acceptance-criteria words appear in their path.

    Deliberately crude and deterministic. Its job is to put plausible files in front of the
    model, not to be clever; the model still decides what to edit.
    """
    words = {w.lower().strip(".,'\"()") for w in ac_text.split() if len(w) > 3}
    scored = []
    for f in files:
        stem = Path(f).stem.lower()
        score = sum(1 for w in words if w in stem or stem in w)
        if "test" in f.lower() or "spec" in f.lower():
            score -= 1                       # prefer showing implementation over existing tests
        scored.append((score, -len(f), f))
    scored.sort(reverse=True)
    return [f for s, _, f in scored if s > 0][:MAX_SHOWN]


def _safe_target(workdir: Path, rel: str) -> Path | None:
    """Confine every write to the working copy. Absolute paths and traversal are rejected."""
    if not rel or rel.startswith(("/", "~")) or ".." in Path(rel).parts:
        return None
    target = (workdir / rel).resolve()
    try:
        target.relative_to(workdir.resolve())
    except ValueError:
        return None
    return target


def generate_code(state: PipelineState) -> dict:
    ac = state["acceptance_criteria"]
    attempts = dict(state.get("attempts", {}))
    attempts["generate_code"] = attempts.get("generate_code", 0) + 1

    analysis = state.get("repo_analysis") or {}
    repo_path = analysis.get("path")
    if not repo_path:
        src = _repo_source(state)
        if src:
            try:
                repo_path = str(_ensure_local(src))
            except Exception as exc:
                print(f"  [Dev Agent] could not obtain the repository: {exc}")
                repo_path = None

    if not repo_path:
        print("  [Dev Agent] no application repository resolved — nothing to implement into")
        return {"code_artifacts": [], "attempts": attempts,
                "dev_note": "no application repository was configured, so no feature was written"}

    # A per-run worktree: the shared clone stays pristine for the analyzer and other runs.
    ws = workspace.app_workdir(repo_path, run_id=state.get("run_id", ""))
    if not ws.get("ok"):
        print(f"  [Dev Agent] {ws.get('error')}")
        return {"code_artifacts": [], "attempts": attempts, "dev_note": ws.get("error")}
    workdir = Path(ws["path"])

    file_map = _file_map(workdir)
    ac_text = str(ac)
    shown = _relevant(workdir, file_map, ac_text)

    context = [f"Stack: {analysis.get('stack') or 'unknown'}"]
    if analysis.get("ui_routes"):
        context.append(f"UI routes: {', '.join(analysis['ui_routes'][:20])}")
    context.append(f"\nRepository files ({len(file_map)} shown):\n" + "\n".join(file_map))
    for rel in shown:
        try:
            body = (workdir / rel).read_text(errors="replace")[:MAX_BYTES]
        except Exception:
            continue
        context.append(f"\n----- CURRENT CONTENTS OF {rel} -----\n{body}")

    failure_context = ""
    for r in reversed(state.get("run_results", [])):
        if r["suite"] == "unit" and r["failed"] > 0:
            failure_context = ("\n\nYour previous attempt failed these unit tests. "
                               f"Fix the implementation:\n{r['failures']}")
            break

    user = (f"Acceptance criteria:\n{ac_text}\n\n" + "\n".join(context) + failure_context)
    raw = call_llm_json("dev_agent", SYSTEM, user)

    artifacts, rejected = [], []
    for f in raw.get("files", []):
        rel = (f.get("path") or "").strip()
        target = _safe_target(workdir, rel)
        if target is None:
            rejected.append(rel or "(empty path)")
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f.get("content", ""))
        artifacts.append({"path": str(target), "repo_path": rel,
                          "description": f.get("description", "")})

    changed = workspace.changed_files(workdir)
    n = attempts["generate_code"]
    print(f"  [Dev Agent] attempt {n}: wrote {len(artifacts)} file(s) into the app repo "
          f"({ws['mode']}); git sees {len(changed)} changed")
    if rejected:
        print(f"  [Dev Agent] rejected {len(rejected)} unsafe path(s): {rejected[:3]}")

    return {"code_artifacts": artifacts, "attempts": attempts,
            "app_workdir": str(workdir),
            "dev_note": raw.get("summary", ""),
            "changed_files": changed}
