"""Run-history analytics — the deterministic half of the Insights agent.

Everything in this module is arithmetic over run records. No model is involved, by design:
the platform's rule is that measurement is code and the model is spent only where judgement
is genuinely required. The agent asks a model to WRITE ABOUT these numbers; it never asks a
model to produce one.

`fold_run` turns a run's persisted events into the handful of facts worth trending, and
`summarize` turns a list of those into the report's figures.
"""
from __future__ import annotations

import json
import statistics
from collections import Counter, defaultdict

_TERMINAL_OK = {"done"}
_SEVERITIES = ("critical", "high", "medium", "low", "info")


# --------------------------------------------------------------------------- folding
def fold_run(run: dict, events: list[dict]) -> dict:
    """One run → the facts worth trending. Mirrors web.server._fold_run, minus the UI shape."""
    st: dict = {}
    nodes: list[str] = []
    # A simulated run carries simulated numbers — p95 640ms, 74.2% coverage. Mixed into a
    # trend they are indistinguishable from measurements, which is exactly the kind of
    # confident-but-unearned reporting this platform exists to prevent. NULL means the run
    # predates provenance tracking: unknown, which is not the same as real.
    mock = run.get("mock")
    mock = None if mock is None else bool(mock)
    for e in events:
        if e.get("type") != "node":
            continue
        try:
            p = json.loads(e["payload"]) if isinstance(e.get("payload"), str) else (e.get("payload") or {})
        except Exception:
            continue
        nodes.append(p.get("node"))
        st.update(p.get("update") or {})

    gates = st.get("gate_decisions") or []
    final_gate: dict[str, str] = {}
    attempts: Counter = Counter()
    for g in gates:
        final_gate[g["gate"]] = g["verdict"]
        attempts[g["gate"]] += 1

    cov = st.get("coverage_report") or None
    after = (cov or {}).get("after") or {}
    coverage = None
    if cov and cov.get("ok"):
        coverage = {"before": cov.get("total_pct"),
                    "after": cov.get("total_pct_after", after.get("total_pct")),
                    "min_pct": cov.get("min_pct")}

    perf = next((r.get("perf") for r in reversed(st.get("run_results") or []) if r.get("perf")), None)
    tests = None
    for r in reversed(st.get("run_results") or []):
        if r.get("passed") is not None or r.get("failed") is not None:
            tests = {"passed": r.get("passed") or 0, "failed": r.get("failed") or 0,
                     "skipped": r.get("skipped") or 0}
            break

    sec = st.get("security_report") or None
    security = None
    if sec and sec.get("ok"):
        meth = sec.get("methodologies") or []
        security = {"total": sec.get("total"), "by_severity": sec.get("by_severity") or {},
                    "ran": sum(1 for m in meth if m.get("ran")),
                    "not_run": sum(1 for m in meth if not m.get("ran"))}

    res = run.get("result") or {}
    usage = res.get("llm_usage") or {}
    t0, t1 = run.get("created_at"), run.get("updated_at")
    inputs = run.get("inputs") or {}
    return {
        "num": run.get("num"), "id": run.get("id"), "mock": mock,
        "flow": run.get("flow") or run.get("mode") or "run",
        "tracks": run.get("tracks") or [],
        "status": run.get("status"),
        "duration_s": round(float(t1) - float(t0), 1) if (t0 and t1) else None,
        "cost_usd": usage.get("cost_usd"),
        "gates": final_gate,
        "gate_attempts": dict(attempts),
        "self_corrections": sum(1 for g in gates if g.get("verdict") == "fail"),
        "coverage": coverage, "perf": perf, "security": security, "tests": tests,
        "repo": inputs.get("source_repo") or inputs.get("repo"),
        "agents_run": len([n for n in nodes if n]),
    }


# --------------------------------------------------------------------------- helpers
def _med(xs: list[float]) -> float | None:
    xs = [x for x in xs if isinstance(x, int | float)]
    return round(statistics.median(xs), 2) if xs else None


def _rate(part: int, whole: int) -> float | None:
    return round(part / whole * 100, 1) if whole else None


def _short(repo: str | None) -> str:
    if not repo:
        return "(unspecified)"
    return str(repo).rstrip("/").split("/")[-1].replace(".git", "")


# --------------------------------------------------------------------------- summary
def summarize(records: list[dict]) -> dict:
    """The figures the report is allowed to quote. Every one is computed here."""
    if not records:
        return {"ok": False, "error": "no runs in the history to analyse"}

    # Simulated runs are excluded outright, and the count is reported. A trend that silently
    # averages mock p95s with measured ones is a fabricated measurement.
    mock_runs = [r for r in records if r.get("mock") is True]
    unknown = [r for r in records if r.get("mock") is None]
    records = [r for r in records if r.get("mock") is not True]
    if not records:
        return {"ok": False, "error": f"every one of the {len(mock_runs)} run(s) in the history "
                                      f"was a mock run — nothing was measured"}

    done = [r for r in records if r["status"] in _TERMINAL_OK]
    blocked = [r for r in records if r["status"] == "blocked"]
    errored = [r for r in records if r["status"] in ("error", "interrupted")]

    # ---- per flow: the reliability table ----
    by_flow: dict[str, list[dict]] = defaultdict(list)
    for r in records:
        by_flow[r["flow"]].append(r)
    flows = []
    for name, rs in sorted(by_flow.items(), key=lambda kv: -len(kv[1])):
        ok = [r for r in rs if r["status"] in _TERMINAL_OK]
        flows.append({
            "flow": name, "runs": len(rs), "completed": len(ok),
            "blocked": sum(1 for r in rs if r["status"] == "blocked"),
            "errored": sum(1 for r in rs if r["status"] in ("error", "interrupted")),
            "completion_rate_pct": _rate(len(ok), len(rs)),
            "median_duration_s": _med([r["duration_s"] for r in rs]),
            "median_cost_usd": _med([r["cost_usd"] for r in rs]),
        })

    # ---- gates: which one actually stops runs ----
    gate_fail: Counter = Counter()
    gate_total: Counter = Counter()
    for r in records:
        for g, v in (r["gates"] or {}).items():
            gate_total[g] += 1
            if v != "pass":
                gate_fail[g] += 1
    gates = [{"gate": g, "evaluated": gate_total[g], "failed": gate_fail.get(g, 0),
              "fail_rate_pct": _rate(gate_fail.get(g, 0), gate_total[g])}
             for g in sorted(gate_total, key=lambda x: -gate_fail.get(x, 0))]

    # ---- coverage: did the gap actually close, and did it stay closed? ----
    cov_runs = [r for r in records if r.get("coverage")]
    cov_by_repo: dict[str, list[dict]] = defaultdict(list)
    for r in cov_runs:
        cov_by_repo[_short(r["repo"])].append(r)
    coverage = [{
        "repo": repo,
        "runs": len(rs),
        "baseline_pct": rs[0]["coverage"]["before"],
        "latest_pct": next((r["coverage"]["after"] for r in reversed(rs)
                            if r["coverage"].get("after") is not None), rs[-1]["coverage"]["before"]),
        "raised": sum(1 for r in rs if r["coverage"].get("after") is not None),
        "series": [{"run": r["num"], "before": r["coverage"]["before"],
                    "after": r["coverage"].get("after")} for r in rs],
    } for repo, rs in sorted(cov_by_repo.items())]

    # ---- performance: is the tail stable across runs? ----
    perf_runs = [r for r in records if r.get("perf")]
    perf = None
    if perf_runs:
        p95 = [r["perf"].get("p95_ms") for r in perf_runs]
        perf = {
            "runs": len(perf_runs),
            "median_p95_ms": _med(p95),
            "worst_p95_ms": max([x for x in p95 if x is not None], default=None),
            "median_p99_ms": _med([r["perf"].get("p99_ms") for r in perf_runs]),
            "p99_measured_in": sum(1 for r in perf_runs if r["perf"].get("p99_ms") is not None),
            "median_throughput_rps": _med([r["perf"].get("throughput_rps") for r in perf_runs]),
            "runs_with_errors": sum(1 for r in perf_runs if (r["perf"].get("error_rate") or 0) > 0),
            "series": [{"run": r["num"], "p95_ms": r["perf"].get("p95_ms"),
                        "p99_ms": r["perf"].get("p99_ms")} for r in perf_runs],
        }

    # ---- security: is the backlog shrinking, and what never ran? ----
    sec_runs = [r for r in records if r.get("security")]
    security = None
    if sec_runs:
        latest, first = sec_runs[-1]["security"], sec_runs[0]["security"]
        security = {
            "runs": len(sec_runs),
            "latest_total": latest.get("total"), "first_total": first.get("total"),
            "delta": (latest.get("total") or 0) - (first.get("total") or 0),
            "by_severity": latest.get("by_severity") or {},
            "disciplines_run": latest.get("ran"), "disciplines_not_run": latest.get("not_run"),
            "series": [{"run": r["num"], "total": r["security"].get("total"),
                        **{s: (r["security"].get("by_severity") or {}).get(s, 0) for s in _SEVERITIES}}
                       for r in sec_runs],
        }

    costs = [r["cost_usd"] for r in records if isinstance(r.get("cost_usd"), int | float)]
    durs = [r["duration_s"] for r in records if isinstance(r.get("duration_s"), int | float)]
    heal = [r["self_corrections"] for r in records]

    return {
        "ok": True,
        "runs_analysed": len(records),
        "mock_runs_excluded": len(mock_runs),
        "provenance_unknown": len(unknown),
        "run_range": {"first": records[0]["num"], "last": records[-1]["num"]},
        "completed": len(done), "blocked": len(blocked), "errored": len(errored),
        "completion_rate_pct": _rate(len(done), len(records)),
        "median_duration_s": _med(durs), "longest_duration_s": max(durs) if durs else None,
        "total_cost_usd": round(sum(costs), 2) if costs else None,
        "median_cost_usd": _med(costs),
        "runs_with_cost": len(costs),
        "self_correcting_runs": sum(1 for h in heal if h),
        "total_self_corrections": sum(heal),
        "flows": flows, "gates": gates,
        "coverage": coverage, "performance": perf, "security": security,
    }


# --------------------------------------------------------------------------- verification
def _numbers_in(obj) -> set[str]:
    """Every figure the summary actually contains, in the spellings a writer would use."""
    out: set[str] = set()

    def norm(v):
        if isinstance(v, bool) or v is None:
            return
        if isinstance(v, int | float):
            out.add(f"{v:g}")
            out.add(f"{round(float(v)):d}")
            out.add(f"{float(v):.1f}")
            out.add(f"{float(v):.2f}")

    def walk(o):
        if isinstance(o, dict):
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
        else:
            norm(o)

    walk(obj)
    return {x.lstrip("+") for x in out}


# A hyphen is a minus sign only when no WORD character precedes it. Both live false alarms
# came from here: "runs 86-89" was read as -89, and "a sub-80% completion rate" as -80. A
# checker that cries wolf is worse than none, because it teaches people to ignore the real
# ones — which are exactly the fabricated figures this exists to catch.
_NUM = __import__("re").compile(r"(?<![\w.])-?\d+(?:\.\d+)?")


def unverified_figures(narrative: str, facts: dict) -> list[str]:
    """Numbers the narrative states that the summary does not contain.

    The whole point of the Insights agent is that a model writes PROSE about numbers it did
    not produce. If a figure appears in the narrative and nowhere in the computed facts, the
    model invented it — which is the same failure as a fabricated pass, in a nicer typeface.
    """
    allowed = _numbers_in(facts)
    bad: list[str] = []
    for tok in _NUM.findall(narrative or ""):
        v = tok.lstrip("+")
        if v in allowed:
            continue
        try:
            f = float(v)
        except ValueError:
            continue
        # a derived rounding of a real figure is fine; an invented one is not
        if any(abs(f - float(a)) < 0.05 for a in allowed if _is_num(a)):
            continue
        bad.append(tok)
    return sorted(set(bad), key=lambda s: (len(s), s))


def _is_num(s: str) -> bool:
    try:
        float(s)
        return True
    except ValueError:
        return False
