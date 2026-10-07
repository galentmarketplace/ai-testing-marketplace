"""The load test must hit the app that was deployed, and report its real error rate.

Both of these were wrong at once, which is why a perfectly healthy deployment produced a
run reporting 0% errors while every single request was refused.
"""
import pytest

from src.agents.perf_agent import _endpoints
from src.state import target_url


# ---------------------------------------------------------------- the target
def test_a_deployed_build_wins_over_the_configured_url():
    """The k6 branch read inputs.base_url and fell back to localhost:8888, then passed that
    to k6 as BASE_URL — overriding the correct URL baked into the generated script."""
    state = {"deployment": {"ok": True, "url": "http://127.0.0.1:51451"},
             "story": {"inputs": {}}}
    assert target_url(state, "http://localhost:8888") == "http://127.0.0.1:51451"


def test_without_a_deployment_the_configured_url_is_used():
    assert target_url({"story": {"inputs": {"base_url": "https://staging.example.com"}}},
                      "http://localhost:8888") == "https://staging.example.com"


def test_the_k6_branch_goes_through_the_shared_helper():
    from pathlib import Path
    code = "\n".join(ln for ln in Path("src/runner/executor.py").read_text().splitlines()
                     if not ln.lstrip().startswith("#"))
    assert "api_base = target_url(" in code, "the k6 target bypasses the precedence helper again"


# ---------------------------------------------------------------- the targets it picks
def test_real_api_endpoints_are_preferred():
    eps = _endpoints({"api": [{"routes": [{"method": "GET", "path": "/api/items"},
                                          {"method": "POST", "path": "/api/items"},
                                          {"method": "GET", "path": "/api/items/{id}"}]}]})
    assert [e["path"] for e in eps] == ["/api/items"], "only safe idempotent GETs, no path params"


def test_a_static_front_end_is_load_tested_over_its_real_routes():
    """Zero API endpoints is not 'nothing to test'. Asking a model to invent endpoints
    produced /users and /orders, which 404'd on every request for 14 minutes."""
    eps = _endpoints({"api": [], "ui_routes": ["/", "/inventory.html", "/cart.html"]})
    assert [e["path"] for e in eps] == ["/", "/inventory.html", "/cart.html"]
    assert all(e["name"] for e in eps), "every target needs a name for its per-endpoint SLO"


def test_a_repo_with_neither_still_tests_the_root():
    assert _endpoints({"api": [], "ui_routes": []}) == [{"name": "root", "path": "/"}]


if __name__ == "__main__":     # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
