"""Phase 0.8 acceptance: every LLM call is costed and attributed to its run and agent."""
import json
import logging

from src import observability as obs
from src import runctx


def test_usage_is_scoped_to_the_run_and_agent():
    tok = runctx.set_run_context(mock=False, run_id="obs-1")
    obs.record_usage("ac_agent", "claude-sonnet-5", 10_000, 1_000)
    obs.record_usage("ui_automation_agent", "claude-sonnet-5", 30_000, 2_000)
    u = obs.usage_for("obs-1")
    runctx.reset_run_context(tok)
    assert u["calls"] == 2 and u["input_tokens"] == 40_000 and u["output_tokens"] == 3_000
    assert u["cost_usd"] > 0 and set(u["by_agent"]) == {"ac_agent", "ui_automation_agent"}
    assert obs.usage_for("some-other-run")["calls"] == 0
    obs.clear_usage("obs-1")
    assert obs.usage_for("obs-1")["calls"] == 0


def test_json_log_carries_the_run_id_and_fields():
    """Format a record through the real handler chain (stream capture is pytest-dependent; the
    formatter + filter are what actually produce the line a log aggregator sees)."""
    obs.setup_logging()
    handler = logging.getLogger().handlers[0]
    rec = logging.LogRecord("atm.test", logging.INFO, __file__, 1, "step finished", None, None)
    rec.extra_fields = {"step": "generate_ac", "criteria": 3}
    tok = runctx.set_run_context(mock=True, run_id="obs-log")
    handler.filters[0].filter(rec)          # the _RunIdFilter stamps the active run
    runctx.reset_run_context(tok)
    out = json.loads(obs.JsonFormatter().format(rec))
    assert out["run_id"] == "obs-log" and out["msg"] == "step finished"
    assert out["step"] == "generate_ac" and out["criteria"] == 3 and out["level"] == "INFO"


def test_run_id_is_dash_without_a_run_context():
    obs.setup_logging()
    rec = logging.LogRecord("atm.test", logging.INFO, __file__, 1, "no run", None, None)
    logging.getLogger().handlers[0].filters[0].filter(rec)
    assert json.loads(obs.JsonFormatter().format(rec))["run_id"] == "-"
