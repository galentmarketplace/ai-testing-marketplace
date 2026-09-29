"""Regression selection: choose from the diff, and only from tags that really exist.

A selector that invents a plausible tag is the quiet disaster here. The filter matches no
test, the suite runs zero cases, and the gate sees no failures — so a change ships having
been regression-tested against nothing at all.
"""
import pytest

from src.agents import regression_agent as ra
from src.integration import workspace


def _spec(tmp_path, body):
    d = tmp_path / "suite"
    d.mkdir(exist_ok=True)
    (d / "a.spec.ts").write_text(body)
    return d


# ------------------------------------------------------------------ tag catalog
def test_real_test_tags_are_catalogued(tmp_path):
    d = _spec(tmp_path, """
import { test } from "@playwright/test";
test("login works @smoke @auth", async () => {});
test("cart totals @regression", { tag: ["@cart"] }, async () => {});
""")
    assert workspace.catalog_tags(d) == ["@auth", "@cart", "@regression", "@smoke"]


def test_npm_scopes_and_jsdoc_are_not_mistaken_for_tags(tmp_path):
    d = _spec(tmp_path, """
import { test } from "@playwright/test";
import x from "@testing-library/react";
/** @param a @returns b @deprecated */
test("plain title with no tags", async () => {});
""")
    assert workspace.catalog_tags(d) == []


def test_gherkin_tags_are_catalogued(tmp_path):
    d = tmp_path / "f"
    d.mkdir()
    (d / "x.feature").write_text("@checkout @slow\nFeature: checkout\n  Scenario: pay\n")
    assert set(workspace.catalog_tags(d)) == {"@checkout", "@slow"}


def test_a_suite_with_no_tags_returns_nothing(tmp_path):
    d = _spec(tmp_path, 'test("untagged", async () => {});')
    assert workspace.catalog_tags(d) == []


# ------------------------------------------------------------------ selection
def _state(tmp_path, tags_in_suite, changed=None):
    suite = _spec(tmp_path, "\n".join(
        f'test("case {i} {t}", async () => {{}});' for i, t in enumerate(tags_in_suite)))
    st = {"story": {"inputs": {"suite_repo": str(suite)}},
          "acceptance_criteria": {"criteria": []}}
    if changed is not None:
        st["changed_files"] = changed
    return st


def test_an_invented_tag_is_dropped_rather_than_run(tmp_path, monkeypatch):
    monkeypatch.setattr(ra, "call_llm_json",
                        lambda *a, **k: {"selected_tags": ["@checkout", "@does-not-exist"],
                                         "reasoning": "r", "risk": "medium"})
    out = ra.select_regression(_state(tmp_path, ["@checkout", "@cart"]))
    assert out["regression_tags"] == ["@checkout"]
    assert out["regression_dropped_tags"] == ["@does-not-exist"]


def test_selecting_only_invented_tags_falls_back_instead_of_running_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(ra, "call_llm_json",
                        lambda *a, **k: {"selected_tags": ["@ghost"], "reasoning": "r", "risk": "high"})
    out = ra.select_regression(_state(tmp_path, ["@cart", "@auth"]))
    assert out["regression_tags"], "an empty selection would run zero tests and report success"
    assert all(t in ("@cart", "@auth") for t in out["regression_tags"])


def test_a_suite_with_no_tags_uses_the_default_and_never_calls_the_model(tmp_path, monkeypatch):
    called = []
    monkeypatch.setattr(ra, "call_llm_json", lambda *a, **k: called.append(1) or {})
    out = ra.select_regression(_state(tmp_path, []))
    assert out["regression_tags"] == ra.DEFAULT_TAGS
    assert called == [], "no catalog means there is nothing to choose between"
    assert "no tags found" in out["regression_rationale"]


def test_configured_tags_extend_the_catalog(tmp_path, monkeypatch):
    seen = {}

    def fake(agent, system, user, **kw):
        seen["user"] = user
        return {"selected_tags": ["@manual-only"], "reasoning": "r", "risk": "low"}
    monkeypatch.setattr(ra, "call_llm_json", fake)
    st = _state(tmp_path, ["@cart"])
    st["story"]["inputs"]["tags"] = "@manual-only"
    out = ra.select_regression(st)
    assert out["regression_tags"] == ["@manual-only"]
    assert "@manual-only" in seen["user"]


def test_the_diff_is_put_in_front_of_the_model(tmp_path, monkeypatch):
    """Impact analysis must reason from the change, not from the ticket prose."""
    seen = {}

    def fake(agent, system, user, **kw):
        seen["user"] = user
        return {"selected_tags": ["@cart"], "reasoning": "touches the cart", "risk": "medium"}
    monkeypatch.setattr(ra, "call_llm_json", fake)
    st = _state(tmp_path, ["@cart", "@auth"], changed=["src/utils/shopping-cart.js"])
    out = ra.select_regression(st)
    assert "src/utils/shopping-cart.js" in seen["user"]
    assert out["regression_tags"] == ["@cart"]
    assert out["regression_risk"] == "medium"


def test_with_no_diff_the_model_is_told_to_widen(tmp_path, monkeypatch):
    seen = {}

    def fake(agent, system, user, **kw):
        seen["user"] = user
        return {"selected_tags": ["@cart"], "reasoning": "r", "risk": "low"}
    monkeypatch.setattr(ra, "call_llm_json", fake)
    ra.select_regression(_state(tmp_path, ["@cart"], changed=[]))
    assert "No diff is available" in seen["user"]
    assert "broader" in seen["user"]


if __name__ == "__main__":     # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
