"""Regression Agent — selects which regression suites a change actually puts at risk.

This used to reason from the story and acceptance criteria alone, which is a guess dressed
as impact analysis: the ticket describes intent, not what the code touched. Two problems
followed from that. Tags were invented that matched no real suite, so the regression run
executed nothing and reported success. And a change whose blast radius was wider than its
ticket suggested silently went untested.

It now selects from evidence:
  * the DIFF of what this run actually changed (files and hunks), not the ticket text
  * a CATALOG of tags that genuinely exist in the suite repository

Selection is constrained to tags in that catalog. When the catalog is empty the agent says
so and falls back to the broad default rather than inventing a filter that silently matches
zero tests — running everything is wasteful, running nothing while claiming success is worse.
"""
from ..integration import workspace
from ..llm import call_llm_json
from ..state import PipelineState

SYSTEM = """You are a QA lead choosing which regression suites to run for a specific change.

You are given the change itself (the diff), the acceptance criteria, and the EXACT list of
tags that exist in the regression suite. Reason from the diff: which areas does this code
actually touch, and what could it plausibly break, including indirectly (shared utilities,
routing, auth, state).

Rules:
- Select ONLY from the provided tag catalog. Never invent a tag: one that matches nothing
  makes the run silently execute zero tests.
- Be selective. Running everything wastes time; missing an impacted area lets bugs through.
- If the diff touches shared or cross-cutting code (auth, routing, storage, utilities),
  widen the selection and say why.
- If the catalog is empty, return an empty list and explain.

Respond with ONLY a JSON object:
{"selected_tags": ["@cart"], "reasoning": "...", "risk": "low|medium|high"}"""

# Used when the suite publishes no tags at all: a filter matching nothing would be worse.
DEFAULT_TAGS = ["@regression"]


def _suite_repo(state: PipelineState) -> str:
    inp = state.get("story", {}).get("inputs", {}) or {}
    a = state.get("repo_analysis") or {}
    return inp.get("suite_repo") or inp.get("repo") or a.get("path") or ""


def select_regression(state: PipelineState) -> dict:
    inp = state.get("story", {}).get("inputs", {}) or {}
    ac = state.get("acceptance_criteria", "(none — regression run against the existing suite)")

    # --- evidence 1: what actually changed ---------------------------------------
    workdir = state.get("app_workdir")
    diff = {"files": [], "diff": "", "truncated": False}
    if workdir:
        try:
            diff = workspace.diff_summary(workdir)
        except Exception as exc:
            print(f"  [Regression Agent] could not read the diff: {exc}")
    changed = diff.get("files") or state.get("changed_files") or []

    # --- evidence 2: which tags genuinely exist ----------------------------------
    catalog: list[str] = []
    suite = _suite_repo(state)
    if suite:
        try:
            catalog = workspace.catalog_tags(suite)
        except Exception as exc:
            print(f"  [Regression Agent] could not read the tag catalog: {exc}")
    configured = [t.strip() for t in str(inp.get("tags") or "").split(",") if t.strip()]
    if configured:
        catalog = sorted(set(catalog) | set(configured))

    if not catalog:
        print("  [Regression Agent] the suite publishes no tags — running the default set "
              f"{DEFAULT_TAGS} rather than a filter that would match nothing")
        return {"regression_tags": DEFAULT_TAGS,
                "regression_rationale": ("no tags found in the suite repository, so the default "
                                         "set was used instead of an invented filter")}

    parts = [f"Story:\n{state.get('story')}", f"\nAcceptance criteria:\n{ac}",
             f"\nTags that exist in the suite ({len(catalog)}): {', '.join(catalog)}"]
    if changed:
        parts.append(f"\nFiles changed by this run ({len(changed)}):\n" + "\n".join(changed[:60]))
    if diff.get("diff"):
        parts.append("\nDiff:\n" + diff["diff"] + ("\n… (truncated)" if diff.get("truncated") else ""))
    if not changed:
        parts.append("\nNo diff is available (this run did not modify code), so select from "
                     "the story and criteria and prefer a broader selection.")

    raw = call_llm_json("regression_agent", SYSTEM, "\n".join(parts))

    # Constrain to reality: a hallucinated tag runs nothing and reports success.
    allowed = {t.lower() for t in catalog}
    chosen = [t for t in (raw.get("selected_tags") or []) if str(t).lower() in allowed]
    dropped = [t for t in (raw.get("selected_tags") or []) if str(t).lower() not in allowed]
    if dropped:
        print(f"  [Regression Agent] dropped {len(dropped)} tag(s) not in the suite: {dropped[:5]}")
    if not chosen:
        chosen = DEFAULT_TAGS if not catalog else catalog[:3]
        print(f"  [Regression Agent] no valid tag selected — falling back to {chosen}")

    risk = raw.get("risk", "unknown")
    print(f"  [Regression Agent] {len(changed)} changed file(s) → {chosen} "
          f"(risk {risk}) — {str(raw.get('reasoning',''))[:80]}…")
    return {"regression_tags": chosen,
            "regression_rationale": raw.get("reasoning", ""),
            "regression_risk": risk,
            "regression_dropped_tags": dropped}
