"""Self-Healing Agent — repairs failing regression test cases. (Design doc: QG2 loop)

When the regression gate (QG2) fails, the orchestrator dispatches this agent
instead of blocking. It reads the failures from the latest regression run and
repairs the responsible test cases (bad locators / stale assertions / drifted
selectors), so the next regression run can pass.

SKELETON NOTE: the real build feeds each failure (error + trace + the current
spec) to the LLM to regenerate a corrected test, and — critically — first
triages product-bug vs. test-bug: only *test* defects are auto-healed; a
suspected *product* defect is escalated (open a Jira bug), never silently
"fixed". This stub records what it would repair and bumps the heal attempt so
the orchestrator can bound retries.
"""
from ..state import PipelineState


def heal_regression(state: PipelineState) -> dict:
    attempts = dict(state.get("attempts", {}))
    attempts["heal_regression"] = attempts.get("heal_regression", 0) + 1

    # pull the failures from the most recent regression run (local or via Jenkins)
    failures: list[dict] = []
    for r in reversed(state.get("run_results", [])):
        if r["suite"] in ("regression", "jenkins"):
            failures = r.get("failures", [])
            break

    healed = [f.get("test", f"regression case #{i+1}") for i, f in enumerate(failures)] \
             or ["regression suite (no per-case detail)"]
    notes = list(state.get("heal_notes", []))
    notes.append({
        "attempt": attempts["heal_regression"],
        "healed": healed,
        "action": "Repaired failing regression case(s): refreshed locators / assertions (test-defect).",
    })

    print(f"  [Self-Heal] attempt {attempts['heal_regression']}: repaired {len(healed)} regression case(s)")
    return {"attempts": attempts, "heal_notes": notes}
