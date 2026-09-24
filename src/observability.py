"""Structured logging + LLM cost accounting.

Two gaps this closes: the platform logged with ~50 bare `print()` calls (no run id, nothing a log
aggregator can filter), and `resp.usage` from every Claude call was discarded, so nobody could say what
a run cost.

`setup_logging()` installs a JSON formatter (one object per line, `ATM_LOG_FORMAT=text` for humans).
Every record carries the active `run_id` from the run context, so a deployment can filter one run out of
a busy server. `record_usage()` accumulates tokens per run and prices them; `usage_for()` reports.
"""
import json
import logging
import os
import threading
import time

from . import runctx

# USD per 1M tokens. Override per deployment with ATM_PRICE_IN / ATM_PRICE_OUT.
PRICE_IN = float(os.environ.get("ATM_PRICE_IN", "3.0"))
PRICE_OUT = float(os.environ.get("ATM_PRICE_OUT", "15.0"))

_lock = threading.Lock()
_usage: dict[str, dict] = {}     # run_id -> {calls, input_tokens, output_tokens, cost_usd, by_agent}


class _RunIdFilter(logging.Filter):
    def filter(self, record):
        ctx = runctx.current()
        record.run_id = ctx.run_id if ctx else "-"
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record):
        out = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)),
               "level": record.levelname, "logger": record.name,
               "run_id": getattr(record, "run_id", "-"), "msg": record.getMessage()}
        for k, v in getattr(record, "extra_fields", {}).items():
            out[k] = v
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)
        return json.dumps(out, default=str)


def setup_logging() -> None:
    root = logging.getLogger()
    if any(isinstance(h.formatter, JsonFormatter) for h in root.handlers):
        return
    h = logging.StreamHandler()
    h.addFilter(_RunIdFilter())
    h.setFormatter(logging.Formatter("%(levelname)s [%(run_id)s] %(name)s: %(message)s")
                   if os.environ.get("ATM_LOG_FORMAT") == "text" else JsonFormatter())
    root.handlers = [h]
    root.setLevel(getattr(logging, os.environ.get("ATM_LOG_LEVEL", "INFO").upper(), logging.INFO))
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


def log(name: str, msg: str, **fields) -> None:
    """One structured line, tagged with the active run."""
    logging.getLogger(name).info(msg, extra={"extra_fields": fields})


# ---------------------------------------------------------------- LLM cost
def record_usage(agent: str, model: str, input_tokens: int, output_tokens: int) -> None:
    ctx = runctx.current()
    rid = (ctx.run_id if ctx else None) or "-"
    cost = (input_tokens / 1e6) * PRICE_IN + (output_tokens / 1e6) * PRICE_OUT
    with _lock:
        u = _usage.setdefault(rid, {"calls": 0, "input_tokens": 0, "output_tokens": 0,
                                    "cost_usd": 0.0, "by_agent": {}})
        u["calls"] += 1
        u["input_tokens"] += input_tokens
        u["output_tokens"] += output_tokens
        u["cost_usd"] = round(u["cost_usd"] + cost, 6)
        a = u["by_agent"].setdefault(agent, {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0})
        a["calls"] += 1
        a["input_tokens"] += input_tokens
        a["output_tokens"] += output_tokens
        a["cost_usd"] = round(a["cost_usd"] + cost, 6)
    log("atm.llm", "llm call", agent=agent, model=model,
        input_tokens=input_tokens, output_tokens=output_tokens, cost_usd=round(cost, 6))


def usage_for(run_id: str | None) -> dict:
    with _lock:
        u = _usage.get(run_id or "-")
        return json.loads(json.dumps(u)) if u else {"calls": 0, "input_tokens": 0, "output_tokens": 0,
                                                    "cost_usd": 0.0, "by_agent": {}}


def clear_usage(run_id: str) -> None:
    with _lock:
        _usage.pop(run_id, None)
