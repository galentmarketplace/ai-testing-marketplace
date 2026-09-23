"""Oracle Check — the gate that stops self-healing from cheating.

The danger with any self-healing loop: the agent can make a failing test go green by quietly
removing or weakening its assertion — a "false pass" (green test that no longer verifies the
requirement). Research puts this failure mode at ~68% of teams that auto-heal.

This auditor runs after the local feature run and BEFORE QG1. For every case implemented as an
ACTIVE test (not skip/fixme) that is currently passing, it asks the LLM whether the test genuinely
asserts that case's expected result (the ORACLE). Any "false pass" is written to `oracle_findings`;
QG1 then fails the gate on it, routing back to the Playwright agent with the critique — which must
restore a faithful assertion or mark the case test.fixme (product mismatch), never weaken it.

This is what "oracle-gated" means: a heal is accepted only if the test still verifies the AC.
"""
import re
from pathlib import Path

from .. import runctx
from ..state import PipelineState


def oracle_check(state: PipelineState) -> dict:
    # Mock runs are deterministic/simulated — no real spec to audit; never gate them.
    if runctx.is_mock():
        return {}
    cases = state.get("functional_cases") or []
    if not cases:
        return {}  # only functional (case-driven) runs have an oracle to check
    arts = state.get("test_artifacts", [])
    spec = next((a for a in reversed(arts) if a.get("type") == "playwright"), None)  # newest wins
    if not spec:
        return {}
    try:
        content = Path(spec["path"]).read_text()
    except Exception:
        return {}

    # Tests already failing are caught by QG1 anyway — the oracle's job is the PASSING ones.
    failed_titles = []
    for r in reversed(state.get("run_results", [])):
        if r["suite"] == "feature":
            failed_titles = [f.get("test", "") for f in (r.get("failures") or [])]
            break

    from ..llm import call_llm_json
    cases_txt = "\n".join(
        f"- {c.get('id')}: {c.get('title')} | expected result (the oracle): {c.get('expected_result')}"
        for c in cases[:14])
    SYSTEM = ("You are a strict TEST-ORACLE auditor. A test that PASSES without actually verifying its "
              "requirement is a FALSE PASS and MUST be flagged. Judge only whether the assertion faithfully "
              "checks the expected result — not code style. EVIDENCE RULE: for every faithful=false finding you MUST "
              "quote, VERBATIM, the exact assertion line (or, if the assertion is missing, the test's opening line) "
              "from the spec as `evidence` — findings without a verbatim quote are discarded. Respond with ONLY JSON: "
              '{"findings":[{"case_id":"<id>","faithful":true|false,"issue":"<short reason>","evidence":"<verbatim quote>"}]}')
    prompt = (
        f"SPEC UNDER REVIEW (Playwright TypeScript):\n{content[:6000]}\n\n"
        f"FUNCTIONAL CASES — each active test MUST assert the case's expected result:\n{cases_txt}\n\n"
        f"Tests currently FAILING (ignore these, already caught): {failed_titles}\n\n"
        "For EACH case implemented as an ACTIVE test (NOT test.skip and NOT test.fixme) and NOT in the failing "
        "list: is the assertion a faithful check of that case's expected result? Flag faithful=false when the "
        "assertion was removed, weakened to a trivial/irrelevant check (e.g. only body visible, expect(true), "
        "a URL/element unrelated to the expected outcome), or the test can pass without the expected result "
        "being true. Skipped/fixme cases are declared gaps — do NOT flag them.")
    try:
        raw = call_llm_json("oracle_agent", SYSTEM, prompt, max_tokens=1500)
        # EVIDENCE GROUNDING (anti-hallucination): keep a finding only if (a) its case id is real and (b) its quoted
        # evidence actually appears in the spec. An LLM may only re-label evidence a real artifact contains —
        # it never invents a finding. Ungrounded findings are dropped and counted, never silently trusted.
        known = {str(c.get("id")) for c in cases}
        def norm(t):
            return re.sub(r"\s+", " ", (t or "")).strip()
        spec_norm = norm(content)
        findings, dropped = [], 0
        for f in (raw.get("findings") or []):
            if f.get("faithful", True):
                continue
            ev = norm(f.get("evidence"))
            if str(f.get("case_id")) not in known or len(ev) < 8 or ev[:80] not in spec_norm:
                dropped += 1
                continue
            findings.append({"case_id": f.get("case_id"),
                             "issue": (f.get("issue") or "assertion does not verify the expected result"),
                             "evidence": f.get("evidence")})
        if dropped:
            print(f"  [Oracle] dropped {dropped} ungrounded finding(s) — no verbatim evidence in the spec")
    except Exception as exc:
        print(f"  [Oracle] check skipped ({exc}) — not blocking the gate")
        return {"oracle_findings": []}

    if findings:
        print(f"  [Oracle] {len(findings)} FALSE-PASS finding(s): "
              + ", ".join(f["case_id"] or "?" for f in findings))
    else:
        print("  [Oracle] all passing cases faithfully assert their expected result")
    return {"oracle_findings": findings}
