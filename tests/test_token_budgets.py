"""No agent may carry an output budget too small for what it has to write.

A truncated reply is not a degraded reply — it is invalid JSON, so the agent crashes and the
run blocks. This happened twice live: the Playwright agent at 6000 tokens truncating a spec
mid-file, and the functional case agent at 5000 truncating a dozen cases. Both blocked the
run before a single test was verified, and both were invisible until a run died.
"""
import re
from pathlib import Path

import pytest

AGENTS = sorted(Path("src/agents").glob("*.py")) + sorted(Path("src/agents/plugins").glob("*.py"))
# Below this, any agent that writes a spec, a case list or a set of verdicts will truncate.
FLOOR = 8000


def _budgets(path: Path):
    """Every max_tokens the file passes, whether literal or an env default."""
    src = path.read_text()
    out = []
    for m in re.finditer(r"max_tokens\s*=\s*(?:int\(os\.environ\.get\([^,]+,\s*\"(\d+)\"\)\)|(\d+))", src):
        out.append(int(m.group(1) or m.group(2)))
    return out


@pytest.mark.parametrize("path", AGENTS, ids=lambda p: p.stem)
def test_no_agent_has_a_budget_that_truncates(path):
    low = [b for b in _budgets(path) if b < FLOOR]
    assert not low, (f"{path.name} passes max_tokens={low}, under the {FLOOR} floor. A truncated "
                     "reply is invalid JSON, so the agent crashes and the run blocks.")


def test_the_shared_default_is_itself_above_the_floor():
    """The hole that let the perf agent block a run: it declared NO budget, so the floor test
    above had nothing to check and passed vacuously while 4000 truncated it mid-script."""
    from src.llm import DEFAULT_MAX_TOKENS
    assert DEFAULT_MAX_TOKENS >= FLOOR, (
        f"agents that declare no max_tokens silently use {DEFAULT_MAX_TOKENS}")


@pytest.mark.parametrize("path", AGENTS, ids=lambda p: p.stem)
def test_every_llm_agent_is_covered(path):
    """Either it declares a budget above the floor, or it inherits a default above it."""
    from src.llm import DEFAULT_MAX_TOKENS
    if "call_llm" not in path.read_text():
        pytest.skip("not an LLM agent")
    budgets = _budgets(path) or [DEFAULT_MAX_TOKENS]
    assert min(budgets) >= FLOOR, f"{path.name} can be truncated at {min(budgets)}"


def test_budgets_are_overridable_without_a_code_change():
    """Operationally this matters: a bigger ticket must not need a release."""
    for name in ("ui_automation_agent", "functional_case_agent", "heal_agent", "oracle_agent"):
        src = (Path("src/agents") / f"{name}.py").read_text()
        assert "os.environ.get" in src and "MAX_TOKENS" in src, f"{name} hard-codes its budget"


def test_large_budgets_take_the_streaming_path():
    """The SDK refuses a non-streaming request above ~8192, so a large budget must stream."""
    from src.llm import _STREAM_ABOVE
    assert _STREAM_ABOVE <= 8192
    for name in ("ui_automation_agent", "heal_agent"):
        assert max(_budgets(Path("src/agents") / f"{name}.py")) > _STREAM_ABOVE


if __name__ == "__main__":     # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
