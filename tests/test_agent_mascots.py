"""Agent mascots are a *system*, not decoration on one screen.

The ask was "leave no place" — so these tests pin every surface that shows an agent to the
shared `mascotFor` / `mascotCrew` helpers. A future edit that re-introduces a bare emoji on
one of them (or adds an agent the mascot engine can't resolve) fails here rather than being
discovered as the one screen that looks different.
"""
import re
from pathlib import Path

import pytest

from src.registry import REGISTRY

DASH = Path(__file__).resolve().parents[1] / "web" / "static" / "index.html"


@pytest.fixture(scope="module")
def html() -> str:
    return DASH.read_text()


# --------------------------------------------------------------- the engine


def test_mascot_engine_is_parametric(html: str) -> None:
    """Body/colour/quirks must come from registry data, so a new agent needs no UI change."""
    assert "function mascotSVG(" in html
    assert "function mascotFor(" in html
    assert "function mascotCrew(" in html
    # built from the manifest, not a hand-written per-agent table
    assert "function buildMascotMeta()" in html
    assert "buildMascotMeta();" in html, "meta is never built — every mascot would fall back"
    # the three registry kinds each get their own body
    for kind in ("gate", "runner"):
        assert f"kind==='{kind}'" in html, f"no distinct body for kind={kind}"


def test_every_face_state_is_emitted(html: str) -> None:
    """All four expressions ship in the SVG; CSS picks one, so live state needs no re-render."""
    for face in ("m-f-idle", "m-f-run", "m-f-pass", "m-f-fail"):
        assert f'class="m-face {face}"' in html, f"missing face {face}"
        assert f".{face}" in html, f"face {face} has no CSS rule to reveal it"


def test_live_graph_state_drives_the_face(html: str) -> None:
    """A running node should look busy and a failed node should look it — without JS plumbing."""
    assert "g.gnode.active .mascot .m-f-run" in html
    assert "g.gnode.done   .mascot .m-f-pass" in html
    assert "g.gnode.fail   .mascot .m-f-fail" in html


def test_motion_is_disabled_when_the_viewer_asks(html: str) -> None:
    """The dashboard already ships a global guard; the mascots add their own belt-and-braces."""
    assert html.count("prefers-reduced-motion: reduce") >= 1
    guards = [
        "animation-duration:.001ms !important",          # pre-existing global guard
        ".mascot, .mascot * { animation:none !important; }",  # mascot-specific
    ]
    assert any(g in html for g in guards), "mascot animation ignores reduced-motion"


# --------------------------------------------------------------- every surface


SURFACES = {
    # label                         marker that proves the surface renders a character
    "agents marketplace card": "${mascotFor(a.id,'idle',40)}",
    "flow-builder palette": "${mascotCrew(crewForTracks([t.track]),18)}",
    "live graph · agent node": "_mg.innerHTML = mascotFor(col.id,'idle',34);",
    "live graph · child node": "_cmg.innerHTML = mascotFor(c.id,'idle',26,{badge:false});",
    "results journey stage": "${mascotFor(st.node,",
    "playbook card crew": "${mascotCrew(crewForFlow(f),22",
    "configure header crew": "mascotCrew(crewForFlow(SELECTED),24)",
    "runs list crew": "${t.length?mascotCrew(crewForTracks(t),18):''}",
    "home · agents in rotation": "mascotCrew(crew,22,'idle',10)",
}


@pytest.mark.parametrize("surface,marker", sorted(SURFACES.items()))
def test_surface_renders_a_mascot(html: str, surface: str, marker: str) -> None:
    assert marker in html, f"{surface} no longer renders an agent mascot"


def test_no_surface_regressed_to_a_bare_emoji(html: str) -> None:
    """The exact emoji-only renders these replaced — if one comes back, a screen looks odd."""
    regressions = [
        """<span class="ai">${a.icon}</span>""",
        """el('text',{class:'nicon'""",
        """el('text',{class:'cicon'""",
        """<span class="jic">${st.ic}</span>""",
    ]
    for r in regressions:
        assert r not in html, f"an agent surface reverted to a bare emoji: {r}"


# --------------------------------------------------------------- data contract


def test_registry_agents_can_all_be_drawn() -> None:
    """mascotSVG switches on `kind`; an unknown kind would silently draw the agent body."""
    kinds = {s.kind for s in REGISTRY}
    assert kinds <= {"agent", "runner", "gate"}, f"unhandled agent kind(s): {kinds}"


def test_every_display_node_carries_what_a_mascot_needs() -> None:
    """colour + icon are the mascot's only inputs besides kind; a blank one draws grey/no badge."""
    missing = [
        (s.id, d.id)
        for s in REGISTRY
        for d in s.display
        if not (d.color or "").startswith("#") or not d.icon
    ]
    assert not missing, f"display nodes without a colour/icon for their mascot: {missing}"


def test_playbook_crew_is_rebuilt_once_the_manifest_lands(html: str) -> None:
    """renderFlows() runs at boot before COLUMNS exist — without this the cards said '0 agents'."""
    boot = html[html.index("fetch('/api/manifest')") :]
    assert "renderFlows();" in boot[:600], "playbook crews never re-render after the manifest"


def test_crew_cannot_overflow_its_card(html: str) -> None:
    """18 characters on one line ran off the Full Pipeline card before this."""
    assert "flex-wrap:wrap" in html[html.index(".m-crew {") : html.index(".m-crew {") + 200]
    assert "m-crew-more" in html, "no overflow indicator — a long crew would just be truncated"


def test_mascot_badge_is_svg_not_emoji(html: str) -> None:
    """MASTER.md forbids emoji-as-icons; the badge is a drawn glyph keyed to the step kind."""
    start = html.index("function mascotSVG(")
    body = html[start : html.index("function mascotFor(")]
    assert "${meta.icon" not in body, "the mascot badge renders an emoji again"
    assert 'class="m-badge-bg"' in body
    assert "const glyph = kind==='gate'" in body, "badge no longer distinguishes step kinds"


# --------------------------------------------------------------- full-body performances


def test_full_body_character_exists(html: str) -> None:
    assert "function mascotFull(" in html
    assert "function mascotFullFor(" in html
    # it reuses the small mascot's face engine rather than drawing a second face
    fn = html[html.index("function mascotFull(") : html.index("function mascotFullFor(")]
    assert "mascotSVG(meta, face, 46" in fn, "the full body draws its own face — two sources of truth"


@pytest.mark.parametrize("pose", ["wave", "work", "run", "stamp"])
def test_each_performance_is_drawn_and_animated(html: str, pose: str) -> None:
    assert f"p==='{pose}'" in html, f"pose {pose} has no artwork"
    assert f".mf-pose-{pose}" in html, f"pose {pose} has no animation"


def test_greeting_and_working_are_offered_to_every_agent(html: str) -> None:
    """The two the user asked for by name must not be kind-gated."""
    fn = html[html.index("function posesFor(") : html.index("function posesFor(") + 400]
    assert "['wave','work']" in fn, "say-hi / get-to-work are no longer universal"
    assert "kind==='runner'" in fn and "kind==='gate'" in fn


def test_performance_picker_is_wired_and_accessible(html: str) -> None:
    assert 'class="ag-pose"' in html and "data-pose=" in html
    assert "aria-pressed" in html, "pose buttons do not report which is active"
    assert ".ag-pose:focus-visible" in html
    assert "stage.innerHTML = mascotFullFor(a.id, 150, null, b.dataset.pose)" in html


def test_work_pose_sits_behind_its_desk(html: str) -> None:
    """The laptop used to render on top of the torso and read as a chest panel."""
    rule = re.search(r"\.mf-pose-work\s+\.mf-leg-l[^{]*\{([^}]*)\}", html)
    assert rule and "display:none" in rule.group(1), "work pose no longer hides the legs"


def test_lane_kind_is_not_hardcoded(html: str) -> None:
    """Every lane claimed kind='agent', so gates performed the agent animation."""
    assert "kind:col.kind||'agent'" in html, "lane mascots ignore their real kind again"


def test_nested_head_keeps_its_state_classes(html: str) -> None:
    """Stripping the head's <svg> also stripped `mascot ms-<state>`, so no face-state rule
    matched and all four faces stacked — the body rendered with red X-eyes while working."""
    fn = html[html.index("function mascotFull(") : html.index("const POSE_ALT = ")]
    assert 'class="mascot ms-${face}"' in fn, "nested head lost its state classes"


def test_motion_stays_in_the_subtle_tier(html: str) -> None:
    """ui-ux-pro-max subtle tier: displacement under ~2px, transform/opacity only."""
    block = html[html.index("Subtle-tier motion") :]
    block = block[: block.index("</style>")]
    for big in ("translateY(-2.5px)", "translateY(-12px)", "rotate(20deg)", "rotate(-162deg)"):
        assert big not in block, f"cartoon-scale motion back in the subtle block: {big}"
    assert "@keyframes m-bob" in block and "translateY(-.7px)" in block


def test_optics_are_lenses_not_eyeballs(html: str) -> None:
    """A bezel ring + small specular highlight is what makes it read as a camera."""
    fn = html[html.index("function mascotSVG(") : html.index("function mascotFull(")]
    assert "const lens = (cx, cls)" in fn
    assert "radialGradient" in fn, "no lens depth"
    assert 'r=".55" fill="#fff"' in fn, "specular highlight lost — reads flat"
