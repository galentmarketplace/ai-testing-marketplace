"""Regression Agent — selects regression suites by tag. (Design doc step 8)

SKELETON NOTE: the real build feeds in the git diff of generated code plus a
catalog of existing tagged regression suites. Here the LLM reasons from the
story + AC tags only.
"""
from ..llm import call_llm_json
from ..state import PipelineState

SYSTEM = """You are a QA lead selecting which regression suites to run for a change.
Given the story and its acceptance criteria, pick the impacted functional tags.
Be selective — running everything wastes time; missing an impacted area lets bugs through.

Respond with ONLY a JSON object:
{"selected_tags": ["@invoice", "@regression"], "reasoning": "..."}"""


def select_regression(state: PipelineState) -> dict:
    # AC is absent on a regression-only run (entry=from_existing_code) — the agent
    # then selects impacted tags from the story/change alone.
    ac = state.get("acceptance_criteria", "(none — regression run against the existing suite)")
    inp = state.get("story", {}).get("inputs", {}) or {}
    ctx = ""
    if inp.get("repo"):
        ctx += f"\nRegression suite repo: {inp['repo']}"
    if inp.get("ci_url"):
        ctx += f"\nCI / Jenkins job: {inp['ci_url']}"
    if inp.get("tags"):
        ctx += f"\nSuites/tags to run: {inp['tags']}"
    user = f"Story:\n{state['story']}\n\nAcceptance criteria:\n{ac}{ctx}"
    raw = call_llm_json("regression_agent", SYSTEM, user)
    print(f"  [Regression Agent] selected {raw['selected_tags']} — {raw['reasoning'][:80]}...")
    return {"regression_tags": raw["selected_tags"]}
