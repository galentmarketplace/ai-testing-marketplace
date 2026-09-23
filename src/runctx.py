"""Per-run execution context — replaces process-global env flags.

Before this module, a run flipped `os.environ["MOCK_LLM"]`/`DEMO_FAIL_SUITE` for the WHOLE process, so a
mock run starting while a live run was mid-flight turned the live run's agents into mock (and vice versa).
Now each run sets a ContextVar at the start of its worker thread; agents/runners read it through the
helpers below. When no run context is set (CLI, CI harness, tests) the helpers fall back to the
environment, so `MOCK_LLM=1 python -m ...` still works unchanged.
"""
import contextvars
import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class RunContext:
    mock: bool = False                      # canned LLM responses + simulated runners
    fail_suites: frozenset = field(default_factory=frozenset)  # demo knob: force these suites to fail once
    real_runner: bool = False               # actually execute Playwright / k6 (deployment capability)
    run_id: str | None = None


_ctx: contextvars.ContextVar[RunContext | None] = contextvars.ContextVar("atm_run_ctx", default=None)


def _env_fail_suites() -> frozenset:
    v = os.environ.get("DEMO_FAIL_SUITE", "").strip()
    if not v:
        return frozenset()
    if v == "all":
        return frozenset({"unit", "feature", "regression"})
    return frozenset(s.strip() for s in v.split(",") if s.strip())


def set_run_context(*, mock: bool, fail_suite: str | None = None, real_runner: bool | None = None,
                    run_id: str | None = None) -> contextvars.Token:
    """Bind the context for the CURRENT thread/task (call at the top of the run worker)."""
    fs = frozenset()
    if fail_suite:
        fs = frozenset({"unit", "feature", "regression"}) if fail_suite == "all" else frozenset({fail_suite})
    rr = (os.environ.get("REAL_RUNNER") == "1") if real_runner is None else real_runner
    return _ctx.set(RunContext(mock=mock, fail_suites=fs, real_runner=rr, run_id=run_id))


def reset_run_context(token: contextvars.Token) -> None:
    _ctx.reset(token)


def current() -> RunContext | None:
    return _ctx.get()


def is_mock() -> bool:
    c = _ctx.get()
    return c.mock if c is not None else os.environ.get("MOCK_LLM") == "1"


def fail_suites() -> frozenset:
    c = _ctx.get()
    return c.fail_suites if c is not None else _env_fail_suites()


def real_runner() -> bool:
    c = _ctx.get()
    return c.real_runner if c is not None else os.environ.get("REAL_RUNNER") == "1"
