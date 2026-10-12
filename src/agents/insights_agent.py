"""Insights agent — natural-language analysis of the platform's OWN run history.

The Julius-AI pattern ("data analysis without the formulas") applied to this product: ask a
question in plain English, get an answer computed from real data. The split is deliberate
and is the whole reason this agent is trustworthy:

  * `integration/analytics.py` COMPUTES every figure — arithmetic over the run store, no model.
  * the model WRITES ABOUT those figures and nothing else.
  * `unverified_figures()` then checks the prose: any number the narrative states that the
    computed facts do not contain is reported, not published quietly.

A model asked to summarise metrics will cheerfully produce a plausible percentage. That is
the same defect as a fabricated test pass, wearing a nicer typeface — so the gate checks it.
"""
import json
import os

from .. import runctx, sandbox
from ..integration import analytics
from ..llm import call_llm_json
from ..mocks import MOCK_RESPONSES
from ..state import GateDecision, PipelineState

MOCK_RESPONSES.setdefault("insights_agent", json.dumps({
    "headline": "Delivery is reliable; the security backlog is not moving",
    "sections": [
        {"title": "Reliability", "body": "Runs complete at a high rate, and the gate that "
                                         "stops work is the one guarding coverage."},
        {"title": "Where the time goes", "body": "Median duration is dominated by the load "
                                                 "test, which boots the application first."}],
    "recommendations": ["Put the coverage gate on the repositories with the lowest baseline",
                        "Enable the dynamic-analysis discipline so the security number means more"]}))

SYSTEM = """You are a QA engineering analyst. You are given a JSON object of figures that were
COMPUTED from a test platform's run history. Write a short, specific analysis of what the
numbers show: reliability, trends, and what deserves attention next.

ABSOLUTE RULE: every number you write must appear in the supplied figures. Do not estimate,
extrapolate, average, or infer a value that is not there. If something is not in the figures,
describe it in words with no number at all. Prefer naming a figure's context ("the coverage
gate") over quoting many numbers.

Be concrete and avoid filler. No greeting, no restatement of the task.
Respond with ONLY JSON:
{"headline":"<one sentence>",
 "sections":[{"title":"<short>","body":"<2-4 sentences>"}],
 "recommendations":["<action>", ...]}"""


def _load_history(limit: int) -> list[dict]:
    """Fold the persisted runs. The store lives in the web layer, so this import is local and
    optional: with no store reachable the agent reports that, rather than inventing a history."""
    try:
        from web import store  # noqa: PLC0415 — optional, layer-crossing
    except Exception:
        return []
    out = []
    try:
        for row in reversed(store.list_runs(None, limit=limit, admin=True)):
            full = store.get_run(row["id"]) or row
            out.append(analytics.fold_run(full, store.events_after(row["id"], 0)))
    except Exception as exc:
        print(f"  [Insights] could not read the run history ({exc})")
        return []
    return out


_MOCK_FACTS = {
    "ok": True, "runs_analysed": 12, "run_range": {"first": 1, "last": 12},
    "completed": 10, "blocked": 2, "errored": 0, "completion_rate_pct": 83.3,
    "median_duration_s": 94.5, "longest_duration_s": 363.0,
    "total_cost_usd": 3.4, "median_cost_usd": 0.28, "runs_with_cost": 12,
    "self_correcting_runs": 4, "total_self_corrections": 6,
    "flows": [{"flow": "Go Code Coverage", "runs": 6, "completed": 6, "blocked": 0, "errored": 0,
               "completion_rate_pct": 100.0, "median_duration_s": 21.0, "median_cost_usd": 0.04}],
    "gates": [{"gate": "COVERAGE", "evaluated": 6, "failed": 0, "fail_rate_pct": 0.0}],
    "coverage": [{"repo": "go-orders-service", "runs": 6, "baseline_pct": 28.6,
                  "latest_pct": 96.4, "raised": 6,
                  "series": [{"run": 1, "before": 28.6, "after": 96.4}]}],
    "performance": None, "security": None,
}


def analyze_runs(state: PipelineState) -> dict:
    inp = state.get("story", {}).get("inputs", {}) or {}
    try:
        limit = max(1, min(500, int(str(inp.get("history_limit") or "50").strip() or 50)))
    except ValueError:
        limit = 50
    question = (inp.get("question") or "").strip()

    if runctx.is_mock():
        facts, records = dict(_MOCK_FACTS), []
    else:
        records = _load_history(limit)
        facts = analytics.summarize(records)

    arts = list(state.get("insights_artifacts", []))
    out_dir = sandbox.run_workspace("insights")
    out_dir.mkdir(parents=True, exist_ok=True)

    if not facts.get("ok"):
        print(f"  [Insights] nothing to analyse — {facts.get('error')}")
        (out_dir / "insights-report.md").write_text(
            f"# Run insights\n\n**Not analysed:** {facts.get('error')}\n")
        arts.append({"type": "insights-report", "path": str(out_dir / "insights-report.md"),
                     "tags": ["@insights"]})
        return {"insights_artifacts": arts, "insights_report": facts}

    print(f"  [Insights] analysing {facts['runs_analysed']} run(s) "
          f"(#{facts['run_range']['first']}–#{facts['run_range']['last']}) …")

    user = (f"FIGURES (the only numbers you may use):\n{json.dumps(facts, indent=1)}\n"
            + (f"\nThe reader specifically asks: {question}\n" if question else ""))
    narrative, note = None, ""
    try:
        narrative = call_llm_json("insights_agent", SYSTEM, user,
                                  max_tokens=int(os.environ.get("ATM_INSIGHTS_MAX_TOKENS", "16000")))
    except Exception as exc:
        note = f"the narrative could not be generated: {exc}"
        print(f"  [Insights] {note}")

    prose = ""
    if narrative:
        prose = "\n".join([narrative.get("headline", "")]
                          + [s.get("body", "") for s in (narrative.get("sections") or [])]
                          + list(narrative.get("recommendations") or []))
    unverified = analytics.unverified_figures(prose, facts) if prose else []
    if unverified:
        print(f"  [Insights] {len(unverified)} figure(s) in the narrative are NOT in the computed "
              f"facts and are flagged as unverified: {unverified}")

    md = _report_markdown(facts, narrative, unverified, question, note)
    (out_dir / "insights-report.md").write_text(md)
    (out_dir / "insights.json").write_text(json.dumps(
        {"facts": facts, "narrative": narrative, "unverified_figures": unverified}, indent=2))
    arts += [{"type": "insights-report", "path": str(out_dir / "insights-report.md"), "tags": ["@insights"]},
             {"type": "insights-json", "path": str(out_dir / "insights.json"), "tags": ["@insights"]}]

    report = {**facts, "narrative": narrative, "unverified_figures": unverified,
              "question": question, "note": note}
    return {"insights_artifacts": arts, "insights_report": report}


def _report_markdown(f: dict, narrative: dict | None, unverified: list[str],
                     question: str, note: str) -> str:
    L = [f"# Run insights — {f['runs_analysed']} run(s), #{f['run_range']['first']}–#{f['run_range']['last']}", ""]
    if question:
        L += [f"**Question:** {question}", ""]
    if narrative:
        L += [f"> {narrative.get('headline', '')}", ""]
        for s in narrative.get("sections") or []:
            L += [f"## {s.get('title', '')}", "", s.get("body", ""), ""]
        if narrative.get("recommendations"):
            L += ["## What to do next", ""] + [f"- {r}" for r in narrative["recommendations"]] + [""]
    elif note:
        L += [f"_No narrative: {note}. The computed figures below are unaffected._", ""]

    L += ["## Verification", ""]
    L += ([f"⚠️ {len(unverified)} figure(s) in the narrative above do not appear in the computed "
           f"data and are **unverified**: `{'`, `'.join(unverified)}`. Treat those sentences as "
           f"the model's wording, not as measurements."] if unverified
          else ["✅ Every figure in the narrative above appears in the computed data below."])

    L += ["", "## Measured", "",
          f"- Completed **{f['completed']}** of {f['runs_analysed']} ({f['completion_rate_pct']}%) · "
          f"blocked {f['blocked']} · errored {f['errored']}",]
    if f.get("mock_runs_excluded"):
        L += [f"- Excluded **{f['mock_runs_excluded']}** simulated run(s): their figures are "
              f"generated, not measured"]
    if f.get("provenance_unknown"):
        L += [f"- ⚠️ **{f['provenance_unknown']}** run(s) predate provenance tracking. They are "
              f"included, but whether they were simulated is unknown — not assumed real"]
    L += [
          f"- Median duration **{f['median_duration_s']}s**, longest {f['longest_duration_s']}s",
          f"- Cost: **${f['total_cost_usd']}** total, ${f['median_cost_usd']} median over "
          f"{f['runs_with_cost']} run(s)",
          f"- Self-correction: {f['self_correcting_runs']} run(s) looped, "
          f"{f['total_self_corrections']} gate failure(s) recovered", ""]

    if f.get("flows"):
        L += ["### By flow", "", "| Flow | Runs | Completed | Blocked | Median | Median cost |",
              "|---|---|---|---|---|---|"]
        L += [f"| {x['flow']} | {x['runs']} | {x['completed']} ({x['completion_rate_pct']}%) | "
              f"{x['blocked']} | {x['median_duration_s']}s | ${x['median_cost_usd']} |"
              for x in f["flows"]]
        L += [""]
    if f.get("gates"):
        L += ["### Gates", "", "| Gate | Evaluated | Failed | Fail rate |", "|---|---|---|---|"]
        L += [f"| {g['gate']} | {g['evaluated']} | {g['failed']} | {g['fail_rate_pct']}% |"
              for g in f["gates"]] + [""]
    for c in f.get("coverage") or []:
        L += [f"### Coverage · {c['repo']}", "",
              f"- Baseline **{c['baseline_pct']}%** → latest **{c['latest_pct']}%** "
              f"across {c['runs']} run(s); {c['raised']} raised it", ""]
    if f.get("performance"):
        p = f["performance"]
        L += ["### Performance", "",
              f"- Median p95 **{p['median_p95_ms']}ms**, worst {p['worst_p95_ms']}ms, "
              f"median p99 {p['median_p99_ms']}ms (measured in {p['p99_measured_in']} of {p['runs']} run(s))",
              f"- Median throughput {p['median_throughput_rps']} rps · "
              f"{p['runs_with_errors']} run(s) saw any error", ""]
    if f.get("security"):
        s = f["security"]
        L += ["### Security", "",
              f"- Latest **{s['latest_total']}** finding(s) vs {s['first_total']} first measured "
              f"(delta {s['delta']:+d}) across {s['runs']} scan(s)",
              f"- {s['disciplines_run']} discipline(s) ran, {s['disciplines_not_run']} did not — "
              f"their findings are unknown, not zero", ""]
    L += ["_Every figure above was computed from the run store; the narrative was written from "
          "these figures and checked against them._"]
    return "\n".join(L)


def insights_gate(state: PipelineState) -> dict:
    """Advisory. The interesting check is not 'did it analyse' but 'did it make anything up'."""
    rep = state.get("insights_report", {}) or {}
    if not rep.get("ok"):
        decision = GateDecision(gate="INSIGHTS", verdict="pass",
                                reason=f"No analysis: {rep.get('error', 'no run history')}",
                                checks=[{"label": rep.get("error", "no run history available"),
                                         "threshold": "runs to analyse", "ok": False, "advisory": True}],
                                route_to=None)
        print(f"  [Gate INSIGHTS] PASS — {decision.reason}")
        return {"gate_decisions": state.get("gate_decisions", []) + [decision.model_dump()]}

    bad = rep.get("unverified_figures") or []
    reason = (f"Analysed {rep['runs_analysed']} run(s); "
              + ("every figure in the narrative is traceable to the computed data."
                 if not bad else
                 f"{len(bad)} figure(s) in the narrative are NOT in the computed data."))
    checks = [
        {"label": f"{rep['runs_analysed']} run(s) analysed", "threshold": "≥ 1", "ok": True},
        {"label": (f"{rep.get('mock_runs_excluded', 0)} simulated run(s) excluded"
                   + (f"; {rep['provenance_unknown']} of unknown provenance included"
                      if rep.get("provenance_unknown") else "")),
         "threshold": "measurements only", "ok": not rep.get("provenance_unknown"), "advisory": True},
        {"label": ("no invented figures" if not bad
                   else f"{len(bad)} unverified figure(s): {', '.join(bad[:6])}"),
         "threshold": "every number computed", "ok": not bad, "advisory": True},
        {"label": f"{len(rep.get('flows') or [])} flow(s) and {len(rep.get('gates') or [])} gate(s) covered",
         "threshold": "report scope", "ok": True, "advisory": True},
    ]
    if rep.get("note"):
        checks.append({"label": rep["note"], "threshold": "narrative", "ok": False, "advisory": True})
    decision = GateDecision(gate="INSIGHTS", verdict="pass", reason=reason, checks=checks, route_to=None)
    print(f"  [Gate INSIGHTS] PASS — {reason}")
    return {"gate_decisions": state.get("gate_decisions", []) + [decision.model_dump()]}
