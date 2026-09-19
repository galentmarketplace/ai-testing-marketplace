"""Shared pipeline state — the data contracts every agent reads/writes.

This is the single most important file in the project. Agents communicate
ONLY through this state; they never call each other directly.
"""
from typing import Literal, TypedDict

from pydantic import BaseModel, Field

# ---------- Contracts (see design doc §4.3) ----------

class Criterion(BaseModel):
    id: str = Field(..., description="e.g. AC-1")
    gherkin: str = Field(..., description="Given/When/Then text")
    priority: Literal["must", "should", "could"] = "must"
    tags: list[str] = []


class AcceptanceCriteria(BaseModel):
    story_id: str
    criteria: list[Criterion]


class TestArtifact(BaseModel):
    type: Literal["playwright", "k6", "jest"]
    path: str
    covers_ac: list[str] = []
    tags: list[str] = []


class CodeArtifact(BaseModel):
    path: str
    description: str = ""


class Failure(BaseModel):
    test: str
    error: str
    trace: str = ""


class PerfMetrics(BaseModel):
    p95_ms: float
    error_rate: float
    p99_ms: float | None = None          # tail latency — the unlucky 1-in-100 (enterprises gate on it too)
    throughput_rps: float | None = None  # achieved requests/sec — the throughput floor
    test_type: str | None = None         # load | stress | soak | spike | breakpoint
    pod_resources: dict | None = None    # per-pod CPU/mem utilization vs limits during the load (deployed target)


class RunResult(BaseModel):
    run_id: str
    suite: str  # unit | feature | perf | regression
    passed: int
    failed: int
    failures: list[Failure] = []
    perf: PerfMetrics | None = None

    @property
    def pass_rate(self) -> float:
        total = self.passed + self.failed
        return 1.0 if total == 0 else self.passed / total


class GateDecision(BaseModel):
    gate: str  # UNIT | QG1 | QG2
    verdict: Literal["pass", "fail"]
    reason: str
    route_to: str | None = None
    attempt: int = 1
    checks: list = []  # [{"label": "pass rate 100%", "threshold": "≥ 95%", "ok": True}] — for the results UI


# ---------- Prescreen / intake (decides which tracks a run activates) ----------

# Track is an open string, not a closed enum — the marketplace adds tracks over time
# (each new agent declares its own in the registry), so RunConfig must accept any of them.
Track = str
ALL_TRACKS: list[Track] = ["unit", "functional", "perf", "regression"]


class RunConfig(BaseModel):
    """The prescreen's output: the *scope* of a run. The orchestrator dispatches
    only the agents a run's tracks require — a regression-only run never touches
    AC/Dev/UI/Perf. `_resolved` marks that the prescreen has expanded a raw
    `mode` into concrete tracks (so the orchestrator runs prescreen exactly once)."""
    mode: Literal["full", "regression", "perf", "smoke", "custom"] = "full"
    tracks: list[Track] = list(ALL_TRACKS)
    entry: Literal["from_story", "from_existing_code"] = "from_story"
    open_pr: bool = True
    reason: str = ""
    resolved: bool = Field(False, alias="_resolved")

    model_config = {"populate_by_name": True}


# ---------- Orchestrator state (flows through the LangGraph graph) ----------

class PipelineState(TypedDict, total=False):
    story: dict                    # {"id": ..., "title": ..., "description": ...}
    run_config: dict               # RunConfig — set by the Prescreen agent; scopes the run
    acceptance_criteria: dict      # AcceptanceCriteria.model_dump()
    functional_cases: list[dict]   # Functional Test Case agent output (derived from the AC / Jira)
    functional_artifacts: list[dict]  # the functional-test-cases document(s)
    code_artifacts: list[dict]     # [CodeArtifact]
    test_artifacts: list[dict]     # [TestArtifact]
    run_results: list[dict]        # [RunResult] (append-only history)
    gate_decisions: list[dict]     # [GateDecision] (append-only history)
    regression_tags: list[str]     # selected by Regression Agent
    heal_notes: list[dict]         # self-healing agent's repair log (on QG2 failure)
    attempts: dict                 # {"generate_code": 1, "generate_ui_scripts": 2, ...}
    pr: dict                       # {"url": ..., "summary": ...}
    status: str                    # running | blocked | done | failed
