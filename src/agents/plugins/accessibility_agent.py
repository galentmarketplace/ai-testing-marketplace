"""Accessibility (a11y) agent — a drop-in marketplace plugin.

Demonstrates the "add N agents" model: this whole capability (agent + runner +
gate) is one file. It self-registers via SPECS and is fully self-contained
(own MOCK_LLM response, own gate threshold), so it needs zero edits elsewhere.
"""
from pathlib import Path

from ...config import GENERATED_DIR
from ...llm import call_llm_json
from ...mocks import MOCK_RESPONSES
from ...registry import AgentSpec, DisplayNode
from ...runner.executor import run_suite
from ...state import GateDecision, PipelineState

MOCK_RESPONSES.setdefault("a11y_agent",
    '{"files": [{"path": "generated/a11y/app.axe.json", "tags": ["@a11y"], '
    '"content": "{\\"standard\\": \\"WCAG21AA\\", \\"rules\\": [\\"color-contrast\\", \\"label\\", \\"aria-roles\\"]}"}]}')

SYSTEM = """You are an accessibility engineer. Given the acceptance criteria (and the target
repo/app), author axe-core accessibility checks covering WCAG 2.1 AA (contrast, labels, roles,
keyboard focus). Respond with ONLY JSON:
{"files": [{"path": "generated/a11y/<name>.axe.json", "tags": ["@a11y"], "content": "..."}]}"""

POLICY = {"max_violations": 0}


def generate_a11y(state: PipelineState) -> dict:
    inp = state.get("story", {}).get("inputs", {}) or {}
    ac = state.get("acceptance_criteria", "(none)")
    ctx = f"\nTarget repo/app: {inp['repo']}" if inp.get("repo") else ""
    raw = call_llm_json("a11y_agent", SYSTEM, f"Acceptance criteria:\n{ac}{ctx}")
    arts = list(state.get("a11y_artifacts", []))
    for f in raw["files"]:
        out = GENERATED_DIR / Path(f["path"]).relative_to("generated")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(f["content"])
        arts.append({"type": "a11y", "path": str(out), "tags": f.get("tags", [])})
    print(f"  [A11y Agent] wrote {len(raw['files'])} accessibility check set(s)")
    return {"a11y_artifacts": arts}


def run_a11y(state: PipelineState) -> dict:
    return run_suite("a11y", state)


def a11y_gate(state: PipelineState) -> dict:
    r = next((x for x in reversed(state.get("run_results", [])) if x["suite"] == "a11y"), None)
    violations = r["failed"] if r else 0
    verdict = "pass" if violations <= POLICY["max_violations"] else "fail"
    checks = [{"label": f"{violations} WCAG violation(s)", "threshold": "0 allowed", "ok": verdict == "pass"}]
    decision = GateDecision(gate="A11Y", verdict=verdict, checks=checks,
                            reason="no accessibility violations" if verdict == "pass" else f"{violations} WCAG violation(s)",
                            route_to="generate_a11y" if verdict == "fail" else None)
    print(f"  [Gate A11Y] {verdict.upper()}")
    return {"gate_decisions": state.get("gate_decisions", []) + [decision.model_dump()]}


_A, _EXEC, _EVAL = "#8b5cf6", "#39c5cf", "#d29922"
SPECS = [
    AgentSpec(id="generate_a11y", label="Accessibility", kind="agent", fn=generate_a11y,
              tracks=("accessibility",), depends_on=("generate_code", "generate_ac"), category="Quality",
              produces="author WCAG 2.1 AA (axe-core) accessibility checks",
              display=[DisplayNode("a11y", "Accessibility", "♿", _A, "A11Y", "agent", column="a11y")]),
    AgentSpec(id="run_a11y", label="A11y Scan", kind="runner", fn=run_a11y,
              tracks=("accessibility",), depends_on=("generate_a11y",), category="Quality",
              produces="run the accessibility scan",
              display=[DisplayNode("a11y_run", "A11y Scan", "⚙️", _EXEC, "EXECUTION", "runner", parent="a11y")]),
    AgentSpec(id="a11y_gate", label="A11Y Gate", kind="gate", fn=a11y_gate,
              tracks=("accessibility",), depends_on=("run_a11y",), gate_name="A11Y",
              on_fail_reset=("generate_a11y", "run_a11y", "a11y_gate"), category="Quality",
              produces="evaluate accessibility violations",
              display=[DisplayNode("a11y_gate", "A11Y Gate", "✅", _EVAL, "EVALUATION", "gate", parent="a11y")]),
]
