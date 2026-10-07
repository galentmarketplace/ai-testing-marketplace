"""A declared SLO that is never measured cannot fail.

The generated k6 script gated on `http_req_duration: ['p(95)<800','p(99)<1500']`, and the
runner read `http_req_duration["p(99)"]` out of k6's JSON summary. But k6's default summary
trend stats are avg/min/med/p(90)/p(95)/max — there is no `p(99)` key. So `p99_ms` was
always None, `quality_gate_1` skipped its p99 criterion on every run, and the Results panel
showed p99 as "—". The tail latency enterprises actually gate on was never checked.
"""

import pytest

from src.gates import quality_gates
from src.state import PerfMetrics, RunResult


def test_the_generated_script_asks_k6_for_p99():
    from src.agents import perf_agent
    src = __import__("inspect").getsource(perf_agent)
    assert "summaryTrendStats" in src, "k6 omits p(99) from the summary unless asked"
    assert "'p(99)'" in src


def test_the_prompt_also_requires_it():
    from src.agents import perf_agent
    assert "summaryTrendStats" in perf_agent.SYSTEM


def test_the_gate_evaluates_p99_when_it_is_present():
    r = RunResult(run_id="t1", suite="feature", passed=10, failed=0,
                  perf=PerfMetrics(p95_ms=100, p99_ms=1900, error_rate=0.0, throughput_rps=50))
    out = quality_gates.quality_gate_1({"run_results": [r.model_dump()], "gate_decisions": [],
                                        "story": {"inputs": {}}})
    d = out["gate_decisions"][-1]
    p99 = [c for c in d["checks"] if c["label"].startswith("p99")]
    assert p99, "a measured p99 produced no criterion"
    assert p99[0]["ok"] is False, "1900ms against a 1500ms policy must fail"


def test_a_missing_p99_is_not_silently_treated_as_a_pass():
    """It is skipped, not passed — but then nothing tells you the tail went unchecked,
    which is why the script must request it in the first place."""
    r = RunResult(run_id="t1", suite="feature", passed=10, failed=0,
                  perf=PerfMetrics(p95_ms=100, p99_ms=None, error_rate=0.0, throughput_rps=50))
    out = quality_gates.quality_gate_1({"run_results": [r.model_dump()], "gate_decisions": [],
                                        "story": {"inputs": {}}})
    labels = [c["label"] for c in out["gate_decisions"][-1]["checks"]]
    assert not any(lbl.startswith("p99") for lbl in labels)


def test_the_runner_reads_p99_from_the_summary():
    from src.runner import executor
    src = __import__("inspect").getsource(executor)
    assert '"p(99)"' in src


if __name__ == "__main__":     # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
