"""The agent detail page is generated from the registry, not written per agent.

That is the whole point: a new agent dropped into REGISTRY must get a complete page with no
UI work. These tests pin the data contract the page depends on, so a registry change that
would leave a page half-empty fails here instead of in front of a customer.
"""
from pathlib import Path

import pytest

from src.registry import BY_ID, REGISTRY, build_manifest

DASH = Path(__file__).resolve().parents[1] / "web" / "static" / "index.html"

DETAIL_FIELDS = (
    "cap", "column", "depends_on", "gate_name", "on_fail_reset", "gates_judging",
    "needs_build", "ship", "final", "post_pr", "superseded_by_jenkins", "trigger_only",
    "max_retries", "steps",
)


@pytest.fixture(scope="module")
def agents() -> list[dict]:
    return build_manifest()["agents"]


@pytest.fixture(scope="module")
def html() -> str:
    return DASH.read_text()


# --------------------------------------------------------------- data contract


def test_every_catalog_agent_can_fill_a_page(agents: list[dict]) -> None:
    missing = {a["id"]: [f for f in DETAIL_FIELDS if f not in a] for a in agents}
    missing = {k: v for k, v in missing.items() if v}
    assert not missing, f"agents whose detail page would have blank sections: {missing}"


def test_wiring_is_rendered_as_names_not_ids(agents: list[dict]) -> None:
    """`depends_on` holds step ids; the page shows them to humans, so they must be labels."""
    ids = set(BY_ID)
    for a in agents:
        for field in ("depends_on", "on_fail_reset"):
            for value in a[field]:
                assert value not in ids, (
                    f"{a['id']}.{field} leaked the raw id {value!r} into the UI"
                )


def test_steps_describe_the_agents_own_lane(agents: list[dict]) -> None:
    """'How it runs' lists the runners/gates nested under this agent's column."""
    playwright = next(a for a in agents if a["id"] == "generate_ui_scripts")
    labels = [s["label"] for s in playwright["steps"]]
    assert "Oracle Check" in labels, "the anti-false-pass auditor vanished from the lane"
    assert "QG1 Gate" in labels
    for step in playwright["steps"]:
        assert step["kind"] in {"agent", "runner", "gate"}
        assert step["color"].startswith("#") and step["icon"]


def test_gate_owners_are_discoverable_from_the_agent_they_judge(agents: list[dict]) -> None:
    """A failure sends work back to the agent — the page states which gate does that."""
    playwright = next(a for a in agents if a["id"] == "generate_ui_scripts")
    assert "QG1" in playwright["gates_judging"]
    gate = next(s for s in REGISTRY if s.gate_name == "QG1")
    assert "generate_ui_scripts" in gate.on_fail_reset, "fixture drifted from the registry"


def test_always_on_agents_report_no_tracks(agents: list[dict]) -> None:
    """Prescreen has no track; the page must say 'always on', never offer to compose it."""
    pre = next(a for a in agents if a["id"] == "prescreen")
    assert pre["tracks"] == []


# --------------------------------------------------------------- the page itself


def test_view_is_registered(html: str) -> None:
    assert 'id="agentView"' in html
    for marker in ("agents:'block', agent:'block'", "agents:'Agents', agent:'Agent'",
                   "'agents','agent'"):
        assert marker in html, f"agent view not registered: {marker}"


def test_cards_are_real_buttons(html: str) -> None:
    """A clickable card must be a button — a div is not reachable by keyboard."""
    assert '<button class="agent-card" type="button" data-agent=' in html
    assert 'aria-label="Open ${escapeHtml(a.label)}"' in html
    assert ".agent-card:focus-visible" in html, "no visible focus ring on a clickable card"
    assert ".agent-card { cursor:pointer;" in html


def test_click_opens_the_detail_page(html: str) -> None:
    assert "b.addEventListener('click', ()=> openAgent(b.dataset.agent))" in html
    assert "function openAgent(" in html
    assert "function renderAgentPage(" in html


def test_side_rail_carries_explanation_and_actions(html: str) -> None:
    """The ask was explicitly: explanation and actions in the side."""
    rail = html[html.index('<aside class="ag-rail">') : html.index("function openAgent(")]
    assert "<h3>When it runs</h3>" in rail
    assert "<h3>Actions</h3>" in rail
    for act in ("compose", "run", "runs", "back"):
        assert f"data-act=\"{act}\"" in rail, f"missing side action: {act}"


def test_every_side_action_is_wired(html: str) -> None:
    body = html[html.index("w.querySelectorAll('.ag-act')") :][:600]
    for act in ("compose", "run", "runs"):
        assert f"act==='{act}'" in body, f"action {act} renders but does nothing"
    assert "openBuilder()" in body and "selectFlow(pbs[0])" in body


def test_rail_does_not_repeat_the_hero_sentence(html: str) -> None:
    """It read as padding when the same `produces` string appeared twice on one screen."""
    rail_start = html.index('<aside class="ag-rail">')
    rail = html[rail_start : html.index("function openAgent(")]
    assert rail.count("escapeHtml(a.produces)") == 0, "side rail duplicates the hero copy"


def test_playbook_shortcuts_actually_navigate(html: str) -> None:
    """The rows advertise 'open →' — they must open something."""
    assert 'data-pb="${escapeHtml(f.id)}"' in html
    assert "FLOWS.find(x=>x.id===row.dataset.pb)" in html


def test_agent_facts_are_derived_not_authored(html: str) -> None:
    """Each sentence must come from a registry flag, so it can never contradict the run."""
    fn = html[html.index("function agentFacts(") : html.index("function renderAgentPage(")]
    for flag in ("trigger_only", "needs_build", "depends_on", "gate_name",
                 "on_fail_reset", "gates_judging", "superseded_by_jenkins", "ship", "final",
                 "post_pr"):
        assert flag in fn, f"agentFacts ignores registry flag {flag!r}"
