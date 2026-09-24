"""Dev Agent — generates feature code + unit tests from AC. (Design doc step 3)

SKELETON NOTE: in the real build this agent gets repo context (relevant IDURAR
source files) and writes actual feature code. Here it generates standalone
files so the pipeline flow is demonstrable.
"""
from pathlib import Path

from .. import sandbox
from ..llm import call_llm_json
from ..state import PipelineState

SYSTEM = """You are a senior MERN developer. Given acceptance criteria, generate
the feature code and Jest unit tests for it.

Respond with ONLY a JSON object:
{"files": [{"path": "generated/code/<name>.js", "description": "...", "content": "..."}]}
Include at least one implementation file and one *.test.js file."""


def generate_code(state: PipelineState) -> dict:
    ac = state["acceptance_criteria"]
    attempts = dict(state.get("attempts", {}))
    attempts["generate_code"] = attempts.get("generate_code", 0) + 1

    # On retry, feed the previous failures back to the agent (self-correction)
    failure_context = ""
    for r in reversed(state.get("run_results", [])):
        if r["suite"] == "unit" and r["failed"] > 0:
            failure_context = f"\n\nPrevious attempt failed these unit tests, fix them:\n{r['failures']}"
            break

    user = f"Acceptance criteria:\n{ac}{failure_context}"
    raw = call_llm_json("dev_agent", SYSTEM, user)

    artifacts = []
    for f in raw["files"]:
        out = sandbox.run_workspace() / Path(f["path"]).relative_to("generated")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(f["content"])
        artifacts.append({"path": str(out), "description": f.get("description", "")})

    print(f"  [Dev Agent] attempt {attempts['generate_code']}: wrote {len(artifacts)} files")
    return {"code_artifacts": artifacts, "attempts": attempts}
