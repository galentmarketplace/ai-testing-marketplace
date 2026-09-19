"""Quality gates — plain code, no LLM. (Design doc §4.4)

Each gate reads the latest RunResult, compares against GATE_POLICY, and appends
a GateDecision to state. Routing functions then read that decision.
"""
from ..config import GATE_POLICY, MAX_RETRIES
from ..state import GateDecision, PipelineState, RunResult


def _latest_result(state: PipelineState, suite: str) -> RunResult:
    for r in reversed(state.get("run_results", [])):
        if r["suite"] == suite:
            return RunResult.model_validate(r)
    raise ValueError(f"No run result found for suite '{suite}'")


def _decide(state: PipelineState, gate: str, suite: str, route_on_fail: str) -> dict:
    policy = GATE_POLICY[gate]
    result = _latest_result(state, suite)

    # Every criterion is recorded (actual vs threshold, pass/fail) so the results UI can show
    # exactly what each gate verified — not just "all criteria met".
    checks = [{
        "label": f"pass rate {result.pass_rate:.0%}",
        "threshold": f"≥ {policy['min_pass_rate']:.0%}",
        "ok": result.pass_rate >= policy["min_pass_rate"],
    }]
    if result.perf and "max_p95_ms" in policy:
        p95, lim = result.perf.p95_ms, policy["max_p95_ms"]
        checks.append({
            "label": f"p95 {p95:.0f}ms" + (f"  ({(lim - p95) / lim:+.0%} margin)" if lim else ""),
            "threshold": f"< {lim}ms", "ok": p95 <= lim,
        })
    # p99 — the tail; enterprises gate on it, not just p95. Margin shows headroom vs the SLA.
    if result.perf and result.perf.p99_ms is not None and "max_p99_ms" in policy:
        p99, lim = result.perf.p99_ms, policy["max_p99_ms"]
        checks.append({
            "label": f"p99 {p99:.0f}ms" + (f"  ({(lim - p99) / lim:+.0%} margin)" if lim else ""),
            "threshold": f"< {lim}ms", "ok": p99 <= lim,
        })
    if result.perf and "max_error_rate" in policy:
        checks.append({
            "label": f"error rate {result.perf.error_rate:.1%}",
            "threshold": f"< {policy['max_error_rate']:.1%}",
            "ok": result.perf.error_rate <= policy["max_error_rate"],
        })
    # Throughput floor — did the app actually sustain the offered load (open-model arrival rate)?
    if result.perf and result.perf.throughput_rps is not None and policy.get("min_throughput_rps"):
        rps, floor = result.perf.throughput_rps, policy["min_throughput_rps"]
        checks.append({
            "label": f"throughput {rps:.0f} req/s", "threshold": f"≥ {floor} req/s", "ok": rps >= floor,
        })

    # ORACLE GATE (QG1): a test that PASSES without asserting its requirement is a FALSE PASS — fail on it,
    # so the heal loop restores a faithful assertion (or marks the case fixme) instead of shipping the cheat.
    if gate == "QG1":
        for f in state.get("oracle_findings", []) or []:
            checks.append({
                "label": f"oracle: {f.get('case_id', 'case')} asserts its expected result",
                "threshold": "must verify AC", "ok": False, "detail": f.get("issue", ""),
            })

    # DEPLOYED per-pod capacity — how hard each pod was hit vs its limits. ADVISORY: a saturated pod is a
    # real capacity finding, but regenerating the k6 script can't fix it, so it informs without looping.
    if result.perf and result.perf.pod_resources:
        pr = result.perf.pod_resources
        if not pr.get("connected"):
            checks.append({"label": "pod metrics — cluster not reachable (kubectl)", "threshold": "advisory", "ok": True, "advisory": True})
        for p in pr.get("pods", [])[:6]:
            cpu, mem = p.get("cpu_peak_pct"), p.get("mem_peak_pct")
            if cpu is not None:
                checks.append({"label": f"{p['pod']}: CPU peak {cpu}% of limit ({p['cpu_peak_m']}/{p['cpu_limit_m']}m)",
                               "threshold": "< 80% limit", "ok": cpu < 80, "advisory": True})
            if mem is not None:
                checks.append({"label": f"{p['pod']}: mem peak {mem}% of limit ({p['mem_peak_mi']}/{p['mem_limit_mi']}Mi)",
                               "threshold": "< 90% limit", "ok": mem < 90, "advisory": True})
            if p.get("restarts"):
                checks.append({"label": f"{p['pod']}: {p['restarts']} restart(s)/OOM under load",
                               "threshold": "0", "ok": False, "advisory": True})

    # Advisory checks inform the report but never fail the gate (so they don't trip the self-heal loop).
    failures = [c for c in checks if not c["ok"] and not c.get("advisory")]
    verdict = "fail" if failures else "pass"
    attempt = state.get("attempts", {}).get(route_on_fail, 1)
    decision = GateDecision(
        gate=gate, verdict=verdict,
        reason="; ".join(f"{c['label']} vs {c['threshold']}" for c in failures) or "all criteria met",
        route_to=route_on_fail if verdict == "fail" else None,
        attempt=attempt, checks=checks,
    )
    print(f"  [Gate {gate}] {verdict.upper()} — {decision.reason}")
    return {"gate_decisions": state.get("gate_decisions", []) + [decision.model_dump()]}


# Gate nodes
def unit_gate(state: PipelineState) -> dict:
    return _decide(state, "UNIT", "unit", route_on_fail="generate_code")


def quality_gate_1(state: PipelineState) -> dict:
    return _decide(state, "QG1", "feature", route_on_fail="generate_ui_scripts")


def quality_gate_2(state: PipelineState) -> dict:
    return _decide(state, "QG2", "regression", route_on_fail="analyze_failure")


# Routing functions (used as conditional edges in the graph)
def _route(state: PipelineState, on_pass: str) -> str:
    decision = state["gate_decisions"][-1]
    if decision["verdict"] == "pass":
        return on_pass
    if decision["attempt"] >= MAX_RETRIES:
        print(f"  [Gate {decision['gate']}] max retries reached — pipeline blocked, human needed")
        return "blocked"
    return decision["route_to"]


def route_after_unit_gate(state: PipelineState) -> str:
    return _route(state, on_pass="generate_scripts")


def route_after_qg1(state: PipelineState) -> str:
    return _route(state, on_pass="select_regression")


def route_after_qg2(state: PipelineState) -> str:
    return _route(state, on_pass="open_pr")
