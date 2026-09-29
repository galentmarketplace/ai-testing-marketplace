"""Self-Healing Agent — triages a failing suite, then repairs ONLY what is safe to repair.

Dispatched by the orchestrator when a quality gate fails, instead of blocking outright.

The dangerous version of this agent is the obvious one: take a failing test and rewrite it
until it passes. That erases real defects. If the application is genuinely broken, "healing"
the test converts a caught bug into a silent regression, which is the exact failure this
platform exists to prevent.

So triage comes first, and it is the whole point:

    test defect     — the test is wrong (stale locator, drifted selector, bad wait,
                      assertion that no longer matches intended behaviour) -> repair it
    product defect  — the app is wrong (the AC says X, the app does Y) -> DO NOT touch the
                      test; escalate so a human sees a real bug
    infrastructure  — the environment failed (app not reachable, timeout, missing build)
                      -> not a code problem; report, do not rewrite anything

Honesty rule: this agent only ever claims what it actually did. If it did not write a file,
it does not report a repair. A previous version appended "repaired N cases" without touching
anything, which made the run history lie.
"""
import json
from pathlib import Path

from .. import runctx
from ..llm import call_llm_json
from ..state import PipelineState

TRIAGE_SYSTEM = """You triage failing end-to-end tests. Decide WHY each failure happened.

You are given the acceptance criteria (the source of truth for intended behaviour), the test
that failed, the error, and the captured failure context (accessibility snapshot of the page
at failure, console output, failed network calls).

Classify each failure as exactly one of:
  "test_defect"    - the application behaves per the acceptance criteria, but the test is
                     wrong: stale/incorrect locator, bad wait, assertion that does not match
                     the criteria, wrong URL or fixture.
  "product_defect" - the application does NOT behave per the acceptance criteria. The test is
                     right and caught a real bug. NEVER classify as test_defect just to make
                     the suite green.
  "infrastructure" - neither: app unreachable, build missing, timeout with no page, auth
                     service down, network failure.

Be conservative. If the evidence does not clearly show the test is at fault, it is NOT a
test_defect. Quote the specific evidence you used.

Respond with ONLY JSON:
{"verdicts": [{"test": "<test title>", "classification": "test_defect|product_defect|infrastructure",
               "reason": "<one sentence>", "evidence": "<quote from the error or context>"}]}"""

REPAIR_SYSTEM = """You repair a Playwright spec whose failures have ALREADY been triaged as
test defects. The application is correct; the test is wrong.

Rules:
- Fix only what the triage identified. Do not weaken or delete assertions to force a pass,
  and do not add try/catch, soft assertions, or unconditional waits to mask a failure.
- Prefer accessible, role-based locators (getByRole/getByLabel/getByText) over brittle CSS.
- Keep every acceptance criterion the spec covers still genuinely asserted.
- Return the COMPLETE corrected spec file, not a diff.

Respond with ONLY JSON:
{"changes": ["<short note per fix>"], "content": "<the full corrected spec file>"}"""

_HEALABLE = "test_defect"


def _latest_failures(state: PipelineState) -> tuple[list[dict], str]:
    for r in reversed(state.get("run_results", [])):
        if r.get("failed", 0) > 0 and r.get("suite") in ("regression", "jenkins", "feature"):
            return list(r.get("failures", [])), r.get("suite", "")
    return [], ""


def _latest_spec(state: PipelineState) -> dict | None:
    for a in reversed(state.get("test_artifacts", [])):
        if a.get("type") == "playwright" and Path(a.get("path", "")).is_file():
            return a
    return None


def _as_dict(f) -> dict:
    return f if isinstance(f, dict) else {"test": getattr(f, "test", ""),
                                          "error": getattr(f, "error", "")}


def heal_regression(state: PipelineState) -> dict:
    attempts = dict(state.get("attempts", {}))
    attempts["heal_regression"] = attempts.get("heal_regression", 0) + 1
    n = attempts["heal_regression"]
    notes = list(state.get("heal_notes", []))
    escalations = list(state.get("heal_escalations", []))

    failures_raw, suite = _latest_failures(state)
    failures = [_as_dict(f) for f in failures_raw]

    if not failures:
        print("  [Self-Heal] no recorded failures to triage — nothing to repair")
        notes.append({"attempt": n, "action": "no failures were recorded, so nothing was repaired",
                      "healed": [], "classification": "none"})
        return {"attempts": attempts, "heal_notes": notes}

    if runctx.is_mock():
        print(f"  [Self-Heal] mock run — would triage {len(failures)} failure(s); nothing rewritten")
        notes.append({"attempt": n, "action": "mock run — triage and repair simulated, no file changed",
                      "healed": [], "classification": "mock", "mock": True})
        return {"attempts": attempts, "heal_notes": notes}

    spec = _latest_spec(state)
    ac = state.get("acceptance_criteria", {})

    # ---------------------------------------------------------------- 1. triage
    ev = "\n\n".join(
        f"TEST: {f.get('test', '?')}\nERROR/CONTEXT:\n{str(f.get('error', ''))[:1800]}"
        for f in failures[:8])
    try:
        triage = call_llm_json(
            "heal_agent_triage", TRIAGE_SYSTEM,
            f"Acceptance criteria (source of truth):\n{json.dumps(ac)[:4000]}\n\n"
            f"Failing suite: {suite}\n\nFailures:\n{ev}")
        verdicts = triage.get("verdicts", []) or []
    except Exception as exc:
        print(f"  [Self-Heal] triage failed ({exc}) — refusing to rewrite tests blind")
        notes.append({"attempt": n, "healed": [], "classification": "error",
                      "action": f"triage failed, so nothing was repaired: {exc}"})
        return {"attempts": attempts, "heal_notes": notes}

    healable = [v for v in verdicts if v.get("classification") == _HEALABLE]
    product = [v for v in verdicts if v.get("classification") == "product_defect"]
    infra = [v for v in verdicts if v.get("classification") == "infrastructure"]

    for v in product:
        escalations.append({"attempt": n, "test": v.get("test", ""), "kind": "product_defect",
                            "reason": v.get("reason", ""), "evidence": v.get("evidence", "")[:500]})
    for v in infra:
        escalations.append({"attempt": n, "test": v.get("test", ""), "kind": "infrastructure",
                            "reason": v.get("reason", ""), "evidence": v.get("evidence", "")[:500]})

    print(f"  [Self-Heal] attempt {n}: triaged {len(verdicts)} failure(s) — "
          f"{len(healable)} test defect(s), {len(product)} product defect(s), {len(infra)} infra")

    if product:
        # A real bug was caught. Repairing the test here would hide it.
        for v in product:
            print(f"     ESCALATED (product defect): {v.get('test', '?')} — {v.get('reason', '')}")
        notes.append({"attempt": n, "healed": [], "classification": "product_defect",
                      "escalated": [v.get("test", "") for v in product],
                      "action": ("suspected PRODUCT defect — tests left untouched and escalated "
                                 "for human review rather than rewritten to pass")})
        return {"attempts": attempts, "heal_notes": notes, "heal_escalations": escalations}

    if not healable:
        reason = "environment/infrastructure failure" if infra else "no failure was attributable to the test"
        print(f"     nothing safely repairable: {reason}")
        notes.append({"attempt": n, "healed": [], "classification": "not_healable",
                      "action": f"no repair attempted — {reason}"})
        return {"attempts": attempts, "heal_notes": notes, "heal_escalations": escalations}

    if not spec:
        print("     test defect(s) found, but no Playwright spec file is available to repair")
        notes.append({"attempt": n, "healed": [], "classification": "test_defect",
                      "action": ("test defect(s) identified but no spec file was available to "
                                 "rewrite, so nothing was repaired")})
        return {"attempts": attempts, "heal_notes": notes, "heal_escalations": escalations}

    # ---------------------------------------------------------------- 2. repair
    spec_path = Path(spec["path"])
    current = spec_path.read_text(errors="replace")
    findings = "\n".join(f"- {v.get('test', '?')}: {v.get('reason', '')} "
                         f"(evidence: {str(v.get('evidence', ''))[:200]})" for v in healable)
    try:
        fix = call_llm_json(
            "heal_agent_repair", REPAIR_SYSTEM,
            f"Acceptance criteria:\n{json.dumps(ac)[:3000]}\n\n"
            f"Triaged test defects to fix:\n{findings}\n\n"
            f"Current spec ({spec_path.name}):\n{current[:14000]}",
            max_tokens=8000)
        content = (fix.get("content") or "").strip()
    except Exception as exc:
        print(f"  [Self-Heal] repair call failed ({exc}) — spec left unchanged")
        notes.append({"attempt": n, "healed": [], "classification": "test_defect",
                      "action": f"repair failed, spec left unchanged: {exc}"})
        return {"attempts": attempts, "heal_notes": notes, "heal_escalations": escalations}

    if not content or content == current:
        print("     the model returned no usable change — spec left unchanged")
        notes.append({"attempt": n, "healed": [], "classification": "test_defect",
                      "action": "no usable correction was produced, so the spec was left unchanged"})
        return {"attempts": attempts, "heal_notes": notes, "heal_escalations": escalations}

    # the repair is only claimed because the file actually changed; keep the POSIX trailing newline
    spec_path.write_text(content if content.endswith("\n") else content + "\n")
    changes = fix.get("changes", []) or ["spec rewritten"]
    healed = [v.get("test", "") for v in healable]
    print(f"  [Self-Heal] rewrote {spec_path.name}: {'; '.join(str(c) for c in changes)[:160]}")

    notes.append({"attempt": n, "healed": healed, "classification": "test_defect",
                  "file": str(spec_path), "changes": changes,
                  "action": f"repaired {len(healed)} test defect(s) in {spec_path.name}"})
    return {"attempts": attempts, "heal_notes": notes, "heal_escalations": escalations}
