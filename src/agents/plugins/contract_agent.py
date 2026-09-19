"""API Contract-testing agent — a drop-in marketplace plugin.

Generates consumer-driven contract tests from the repo's API surface, runs them,
and gates on schema/breaking-change violations. One file = one capability.
"""
from pathlib import Path

from ...config import GENERATED_DIR
from ...llm import call_llm_json
from ...mocks import MOCK_RESPONSES
from ...registry import AgentSpec, DisplayNode
from ...runner.executor import run_suite
from ...state import GateDecision, PipelineState

MOCK_RESPONSES.setdefault("contract_agent",
    '{"files": [{"path": "generated/contract/api.pact.json", "tags": ["@contract"], '
    '"content": "{\\"consumer\\": \\"web\\", \\"provider\\": \\"api\\", \\"interactions\\": 6}"}]}')

SYSTEM = """You are an API testing engineer. Given the acceptance criteria and the target repo,
derive the API surface and author consumer-driven contract tests (Pact-style) that assert request/
response schemas and catch breaking changes. Respond with ONLY JSON:
{"files": [{"path": "generated/contract/<name>.pact.json", "tags": ["@contract"], "content": "..."}]}"""

POLICY = {"max_breaking": 0}


def generate_contract(state: PipelineState) -> dict:
    inp = state.get("story", {}).get("inputs", {}) or {}
    ac = state.get("acceptance_criteria", "(none)")
    ctx = f"\nTarget repo: {inp['repo']}" if inp.get("repo") else ""
    ctx += f"\nAPI base URL: {inp['base_url']}" if inp.get("base_url") else ""
    raw = call_llm_json("contract_agent", SYSTEM, f"Acceptance criteria:\n{ac}{ctx}")
    arts = list(state.get("contract_artifacts", []))
    for f in raw["files"]:
        out = GENERATED_DIR / Path(f["path"]).relative_to("generated")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(f["content"])
        arts.append({"type": "contract", "path": str(out), "tags": f.get("tags", [])})
    print(f"  [Contract Agent] wrote {len(raw['files'])} contract(s)")
    return {"contract_artifacts": arts}


def run_contract(state: PipelineState) -> dict:
    return run_suite("contract", state)


def contract_gate(state: PipelineState) -> dict:
    r = next((x for x in reversed(state.get("run_results", [])) if x["suite"] == "contract"), None)
    breaking = r["failed"] if r else 0
    verdict = "pass" if breaking <= POLICY["max_breaking"] else "fail"
    checks = [{"label": f"{breaking} breaking change(s)", "threshold": "0 allowed", "ok": verdict == "pass"}]
    decision = GateDecision(gate="CONTRACT", verdict=verdict, checks=checks,
                            reason="contracts honoured" if verdict == "pass" else f"{breaking} breaking change(s)",
                            route_to="generate_contract" if verdict == "fail" else None)
    print(f"  [Gate CONTRACT] {verdict.upper()}")
    return {"gate_decisions": state.get("gate_decisions", []) + [decision.model_dump()]}


_A, _EXEC, _EVAL = "#2dd4bf", "#39c5cf", "#d29922"
SPECS = [
    AgentSpec(id="generate_contract", label="API Contract", kind="agent", fn=generate_contract,
              tracks=("contract",), depends_on=("generate_code", "generate_ac"), category="API",
              produces="derive consumer-driven contract tests from the API surface",
              display=[DisplayNode("contract", "API Contract", "🔌", _A, "CONTRACT", "agent", column="contract")]),
    AgentSpec(id="run_contract", label="Contract Run", kind="runner", fn=run_contract,
              tracks=("contract",), depends_on=("generate_contract",), category="API",
              produces="run the contract tests",
              display=[DisplayNode("contract_run", "Contract Run", "⚙️", _EXEC, "EXECUTION", "runner", parent="contract")]),
    AgentSpec(id="contract_gate", label="CONTRACT Gate", kind="gate", fn=contract_gate,
              tracks=("contract",), depends_on=("run_contract",), gate_name="CONTRACT",
              on_fail_reset=("generate_contract", "run_contract", "contract_gate"), category="API",
              produces="evaluate schema / breaking-change violations",
              display=[DisplayNode("contract_gate", "CONTRACT Gate", "✅", _EVAL, "EVALUATION", "gate", parent="contract")]),
]
