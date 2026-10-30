"""The home dashboard's metrics are derived in the browser from the run list — nothing else.

Two things can quietly break this screen, and neither shows up as an error:

1. A metric starts reading a field `list_runs()` does not return. The tile renders `undefined`
   or `NaN` and nobody notices until a customer is looking at it.
2. A metric starts claiming more than the data supports. `/api/runs` carries no gate verdicts,
   so "Completed" means *the run reached the end* — not *every gate went green*. A "pass rate"
   on this screen would be exactly the false pass this whole platform exists to prevent.

These tests pin both.
"""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DASH = ROOT / "web" / "static" / "index.html"
STORE = ROOT / "web" / "store.py"


@pytest.fixture(scope="module")
def html() -> str:
    return DASH.read_text()


@pytest.fixture(scope="module")
def metrics(html: str) -> str:
    """The three renderers that make up the dashboard's metric layer."""
    start = html.index("// ---- Home dashboard metrics ")
    return html[start : html.index("function refreshRuns(")]


@pytest.fixture(scope="module")
def run_fields() -> set[str]:
    """Exactly what /api/runs hands the browser, read off the SELECT in list_runs()."""
    src = STORE.read_text()
    cols = re.search(r'cols = "([^"]+)"', src[src.index("def list_runs("):]).group(1)
    return {c.strip().split(" AS ")[-1].strip() for c in cols.split(",")}


# --------------------------------------------------------------- the data contract


def test_every_metric_reads_a_field_the_api_returns(metrics: str, run_fields: set[str]) -> None:
    """A tile that reads a field list_runs() never selects renders `undefined`, silently."""
    used = {m for m in re.findall(r"\br\.([a-z_]+)", metrics)}
    unknown = used - run_fields
    assert not unknown, f"home metrics read fields /api/runs does not return: {sorted(unknown)}"


def test_metrics_add_no_network_calls(metrics: str) -> None:
    """The home view polls /api/runs every few seconds. One fetch per row would be a behaviour
    change wearing a UI change's clothes."""
    for call in ("fetch(", "XMLHttpRequest", "EventSource"):
        assert call not in metrics, f"the home metrics started calling {call}"


def test_renderers_are_driven_by_the_existing_poll(html: str) -> None:
    body = html[html.index("function renderHomeStats(") :][:700]
    assert "renderHomeActivity(runs)" in body and "renderHomeMix(runs)" in body, (
        "the panels are not fed by renderHomeStats — they would never update"
    )


# --------------------------------------------------------------- the honesty contract


def test_no_pass_rate_is_claimed(metrics: str) -> None:
    """Gate verdicts are not in this payload, so no figure here may be labelled a pass rate.

    Comments are stripped first — the block explains *why* there is no pass rate, and that
    explanation should not be what keeps the test green."""
    code = "\n".join(ln for ln in metrics.splitlines() if not ln.lstrip().startswith("//"))
    for phrase in ("Pass rate", "pass rate", "Success rate", "success rate", "Passed", "passed"):
        assert phrase not in code, f"home dashboard claims {phrase!r} without gate data"


def test_completion_rate_always_prints_its_denominator(metrics: str) -> None:
    """A bare percentage invites the reading 'N% of everything'. The base is always shown."""
    tile = metrics[metrics.index("k:'Completed'") - 200 : metrics.index("k:'Completed'") + 220]
    assert "done/settled*100" in tile.replace(" ", "")
    assert "finished run" in tile, "the % is printed without the run count it is a share of"


def test_the_50_row_cap_is_disclosed(metrics: str) -> None:
    """list_runs() returns the most recent 50; otherwise every rate silently implies all-time."""
    assert "runs.length>=50" in metrics and "most recent 50" in metrics


def test_duration_excludes_runs_still_going(metrics: str) -> None:
    """updated_at on a live run is 'so far', not a duration — including it drags the median down."""
    line = next(ln for ln in metrics.splitlines() if "const durs=" in ln)
    assert "!_isLive(r)" in line, "median duration counts in-flight runs as if they had finished"


# --------------------------------------------------------------- the chart


def test_chart_refuses_to_plot_too_few_points(metrics: str) -> None:
    """The project's charting rule: under 4 data points, show the number, not a trend."""
    assert "if(active<4)" in metrics
    assert "too few points to plot a trend" in metrics


def test_bar_values_are_readable_without_hovering(metrics: str) -> None:
    assert 'class="spk-n"' in metrics, "bar counts are not drawn as text"
    assert 'role="img"' in metrics and "aria-label=" in metrics, "the chart has no text alternative"


def test_chart_colors_come_from_the_tokens(metrics: str) -> None:
    """Status colour is the design system's, not a hex typed into a renderer."""
    head = metrics[metrics.index("const head=") : metrics.index("const active=")]
    assert "var(--ok)" in head and "var(--fail)" in head


# --------------------------------------------------------------- presentation


def test_tiles_are_numeric_and_aligned(html: str) -> None:
    assert ".stat-card .v { font-variant-numeric:tabular-nums; }" in html, (
        "stat values are not tabular — digits jitter as the poll updates them"
    )


def test_tile_grid_fills_its_rows(html: str) -> None:
    """Eight tiles on an auto-fit grid left an orphan row of two."""
    assert "@media (min-width:1080px) { .home-stats { grid-template-columns:repeat(4,1fr); } }" in html


def test_agent_crew_uses_the_shared_registry_helpers(metrics: str) -> None:
    """'Agents in rotation' must resolve through the registry, so a new agent needs no edit here."""
    assert "crewForTracks(" in metrics and "mascotCrew(" in metrics
