"""0.4 acceptance: a mock run and a live run executing concurrently must not see each other's flags."""
import os
import threading
import time

from src import runctx


def test_env_fallback_when_no_context(monkeypatch):
    monkeypatch.setenv("MOCK_LLM", "1")
    assert runctx.is_mock() is True
    monkeypatch.delenv("MOCK_LLM")
    assert runctx.is_mock() is False


def test_context_overrides_env(monkeypatch):
    monkeypatch.setenv("MOCK_LLM", "1")
    tok = runctx.set_run_context(mock=False, fail_suite="feature", run_id="r1")
    try:
        assert runctx.is_mock() is False
        assert runctx.fail_suites() == {"feature"}
        assert runctx.current().run_id == "r1"
    finally:
        runctx.reset_run_context(tok)
    assert runctx.is_mock() is True


def test_concurrent_runs_do_not_bleed(monkeypatch):
    monkeypatch.delenv("MOCK_LLM", raising=False)
    seen = {}

    def worker(name, mock, fail):
        runctx.set_run_context(mock=mock, fail_suite=fail, run_id=name)
        time.sleep(0.05)                       # overlap with the other worker
        seen[name] = (runctx.is_mock(), set(runctx.fail_suites()))

    t1 = threading.Thread(target=worker, args=("mock-run", True, "all"))
    t2 = threading.Thread(target=worker, args=("live-run", False, None))
    t1.start(); t2.start(); t1.join(); t2.join()
    assert seen["mock-run"] == (True, {"unit", "feature", "regression"})
    assert seen["live-run"] == (False, set())
    assert runctx.is_mock() is False           # main thread untouched
    assert "MOCK_LLM" not in os.environ        # nothing leaked into process env
