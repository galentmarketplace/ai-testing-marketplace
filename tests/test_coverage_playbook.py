"""The Go Code Coverage playbook must be able to deliver the fix it generates.

The track generated tests, verified them and raised coverage — then the playbook stopped,
because its card declared `tracks:['coverage']` and the delivery agents are
`tracks=("delivery",)`. So from the UI you got the measurement and the fix and no pull
request, while docs/DEMO.md told the presenter to run it "with the delivery track on" —
a toggle that did not exist on the playbook.
"""
import re
from pathlib import Path

import pytest

HTML = Path("web/static/index.html").read_text()
# the card entry spans several lines (description + chips)
CARD = HTML.split("{id:'coverage',", 1)[1].split("chips:[", 1)[0] \
     + HTML.split("{id:'coverage',", 1)[1].split("chips:[", 1)[1].split("]}", 1)[0]
INTAKE = HTML.split("coverage:{fields:[", 1)[1].split("]},", 1)[0]


def test_the_playbook_includes_the_delivery_track():
    assert "tracks:['coverage','delivery']" in CARD


def test_the_intake_can_name_a_destination():
    """Without this there is nowhere to say where the fix should land."""
    assert "FIELD('dest_repo'" in INTAKE


def test_the_destination_is_optional_so_measure_only_still_works():
    m = re.search(r"FIELD\('dest_repo','[^']*','text',(true|false)", INTAKE)
    assert m and m.group(1) == "false", "requiring a destination would break measure-only runs"


def test_the_card_describes_the_fix_and_the_pr_not_just_measurement():
    for claim in ("CLOSE the gap", "re-measure", "pull request"):
        assert claim in CARD, f"the card does not mention {claim!r}"
    assert "Go Test Gen" in CARD and "PR" in CARD


def test_the_card_says_how_to_opt_out_of_the_pr():
    assert "blank" in CARD and "measure only" in CARD


def test_the_destination_field_survives_a_hidden_picker():
    """The picker is hidden whenever a Configuration supplies the repo. Keying the text
    field off GH.connected alone left such a Configuration unable to name a destination."""
    body = HTML.split("function fieldsFor()", 1)[1].split("\nfunction ", 1)[0]
    assert "if(pickersVisible()) fs = fs.filter(f=>f.id!=='dest_repo');" in body
    assert "if(GH.connected) fs = fs.filter(f=>f.id!=='repo');" in body


def test_one_definition_decides_whether_the_pickers_are_on_screen():
    """The card and the intake must agree, or a field is dropped with nothing replacing it."""
    assert "function pickersVisible()" in HTML
    assert HTML.count("pickersVisible()") >= 3      # definition + card + intake


def test_a_configuration_that_carries_a_destination_provides_that_field():
    prov = HTML.split("function providedFields(p)", 1)[1].split("\nfunction ", 1)[0]
    assert "if(c.dest_repo) s.add('dest_repo');" in prov


def test_the_configuration_hint_names_the_repository_it_supplies():
    """"✓ source repo" told you a repo was supplied, not that it was the wrong one for the
    playbook you had just picked — the run then analysed a repo you never saw named."""
    body = HTML.split("function renderCfgProvides()", 1)[1].split("\nfunction ", 1)[0]
    assert "_PROV_VALUE" in body and "c.source_repo" in body and "c.dest_repo" in body


def test_the_agent_chips_name_child_agents_too():
    body = HTML.split("function renderScopeSummary()", 1)[1].split("\nfunction ", 1)[0]
    assert "c.children" in body, "Go Test Gen — the agent that writes the tests — went unlisted"
    assert ".scope-chips span.sub" in HTML, "child chips have no style"


def test_the_min_coverage_placeholder_matches_the_documented_demo():
    assert "FIELD('min_coverage','Minimum coverage %','text',false,'80'" in INTAKE


def test_the_runbook_no_longer_asks_for_a_toggle_that_does_not_exist():
    demo = Path("docs/DEMO.md").read_text()
    assert "with the **delivery** track on" not in demo


if __name__ == "__main__":     # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
