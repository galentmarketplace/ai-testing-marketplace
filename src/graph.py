"""The Orchestrator — a LangGraph graph wiring all agents, runners, and gates.

This IS design doc §4.2. Read it top to bottom alongside that diagram.
"""
from langgraph.graph import END, StateGraph

from .agents.ac_agent import generate_ac
from .agents.dev_agent import generate_code
from .agents.perf_agent import generate_perf_scripts
from .agents.pr_agent import open_pr
from .agents.regression_agent import select_regression
from .agents.ui_automation_agent import generate_ui_scripts
from .gates.quality_gates import (
    quality_gate_1,
    quality_gate_2,
    route_after_qg1,
    route_after_qg2,
    route_after_unit_gate,
    unit_gate,
)
from .runner.executor import execute_regression, execute_scripts, run_unit_tests
from .state import PipelineState


def generate_scripts(state: PipelineState) -> dict:
    """UI + Perf script generation. Sequential here for simplicity;
    LangGraph supports true parallel fan-out once you need it."""
    update = generate_ui_scripts(state)
    merged = {**state, **update}
    update2 = generate_perf_scripts(merged)
    return {**update, **update2}


def analyze_failure(state: PipelineState) -> dict:
    """Regression failed beyond retries — in the real build, this agent
    diagnoses whether it's a product bug (open Jira defect) or a bad test
    (route back to script agent). Stubbed as a human handoff."""
    print("  [Analyze] regression failure needs human review — creating Jira defect (stub)")
    return {"status": "blocked"}


def blocked(state: PipelineState) -> dict:
    print("  [Orchestrator] pipeline BLOCKED — human intervention required")
    return {"status": "blocked"}


def build_graph():
    g = StateGraph(PipelineState)

    # Nodes: agents think, runners act, gates judge
    g.add_node("generate_ac", generate_ac)
    g.add_node("generate_code", generate_code)
    g.add_node("run_unit_tests", run_unit_tests)
    g.add_node("unit_gate", unit_gate)
    g.add_node("generate_scripts", generate_scripts)
    g.add_node("execute_scripts", execute_scripts)
    g.add_node("quality_gate_1", quality_gate_1)
    g.add_node("select_regression", select_regression)
    g.add_node("execute_regression", execute_regression)
    g.add_node("quality_gate_2", quality_gate_2)
    g.add_node("open_pr", open_pr)
    g.add_node("analyze_failure", analyze_failure)
    g.add_node("blocked", blocked)

    # Edges: the pipeline flow with gate-driven routing
    g.set_entry_point("generate_ac")
    g.add_edge("generate_ac", "generate_code")           # TODO: human-approval interrupt here
    g.add_edge("generate_code", "run_unit_tests")
    g.add_edge("run_unit_tests", "unit_gate")
    g.add_conditional_edges("unit_gate", route_after_unit_gate,
                            {"generate_scripts": "generate_scripts",
                             "generate_code": "generate_code",
                             "blocked": "blocked"})
    g.add_edge("generate_scripts", "execute_scripts")
    g.add_conditional_edges("quality_gate_1", route_after_qg1,
                            {"select_regression": "select_regression",
                             "generate_ui_scripts": "generate_scripts",
                             "blocked": "blocked"})
    g.add_edge("execute_scripts", "quality_gate_1")
    g.add_edge("select_regression", "execute_regression")
    g.add_edge("execute_regression", "quality_gate_2")
    g.add_conditional_edges("quality_gate_2", route_after_qg2,
                            {"open_pr": "open_pr",
                             "analyze_failure": "analyze_failure",
                             "blocked": "blocked"})
    g.add_edge("open_pr", END)
    g.add_edge("analyze_failure", END)
    g.add_edge("blocked", END)

    return g.compile()
