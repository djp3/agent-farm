"""A name too long for the name column gives up its middle, so its end stays in sight (#25):
session names often end in what tells them apart, such as a PR number. Names are made up."""
import pytest

import main
from collector import Agent, Instance, Shell


def test_a_long_name_keeps_its_start_and_its_end():
    assert main._fit_name("acme-pasture-rotation-review-fixes-pr-87", 26) == "acme-pasture-…-fixes-pr-87"
    assert main._fit_name("weather-station-frost-alert-pr-204", 26) == "weather-stati…alert-pr-204"


def test_a_name_that_fits_is_padded_to_the_column_and_never_cut():
    assert main._fit_name("barn-door", 12) == "barn-door   "
    assert main._fit_name("exactly-twelve", 14) == "exactly-twelve"


def test_without_a_column_a_name_is_left_whole():
    """Align activity text off: no field, nothing cut here (the row is cut at the pane's edge)."""
    assert main._fit_name("acme-pasture-rotation-review-fixes-pr-87", 0) == "acme-pasture-rotation-review-fixes-pr-87"


@pytest.mark.parametrize("width", range(1, 40))
def test_a_cut_name_fills_its_field_exactly(width):
    """So every summary still starts in one column."""
    fitted = main._fit_name("acme-orchard-irrigation-and-drainage-pr-112", width)
    assert len(fitted) == width
    assert fitted.count("…") == 1


def test_a_narrow_field_still_keeps_the_last_six_characters():
    """A subagent's field shrinks under the tree's guides; the end keeps room for "pr-112"
    while the start keeps a character of its own."""
    assert main._fit_name("acme-orchard-irrigation-pr-112", 8) == "a…pr-112"
    assert main._fit_name("acme-orchard-irrigation-pr-112", 3) == "a…2"
    assert main._fit_name("acme-orchard-irrigation-pr-112", 2) == "a…"
    assert main._fit_name("acme-orchard-irrigation-pr-112", 1) == "…"


def test_every_kind_of_row_cuts_its_name_the_same_way():
    session = Instance(pid=1, name="acme-pasture-rotation-review-fixes-pr-87", status="idle")
    assert main.instance_label(session, 0.0, name_width=26).plain.startswith("acme-pasture-…-fixes-pr-87")
    agent = Agent("a1", "reviewer", "code-reviewer-for-the-sensor-driver", "Review it")
    assert main.agent_label(agent, 0.0, name_width=20).plain.startswith("reviewer (…r-driver)")
    shell = Shell(9001, 0.0, "npm run test:watch -- --reporter=dot", "Watch the tests")
    assert main.shell_label(shell, 0.0, name_width=20).plain.startswith("npm run te…orter=dot")
