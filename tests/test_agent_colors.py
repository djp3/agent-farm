"""Rows color a name by state, so a teammate's own color (the one Claude Code gives it) is
shown in the detail pane, as Claude Code draws it."""
import main
from collector import Agent


def test_names_follow_state_and_the_teammate_color_is_in_the_detail_pane():
    a = Agent(agent_id="a", name="daily-review-cleanup", color="blue", status="dormant")
    name = main.agent_label(a, 0.0).spans[0]
    assert name.style == f"bold {main.BLUE}"
    detail = main.detail_text(a, 0.0)
    assert "color     blue" in detail.plain
    assert any(sp.style == "#6a9bcc" for sp in detail.spans)         # Claude Code's blue, not ANSI's
    pink = main.detail_text(Agent(agent_id="b", name="b", color="pink"), 0.0)
    assert any(sp.style == "#c46686" for sp in pink.spans)
    # the meta file is another program's: anything but a known color name is not shown
    odd = main.detail_text(Agent(agent_id="c", name="c", color="link https://x.test"), 0.0)
    assert "color" not in odd.plain and "x.test" not in odd.plain
