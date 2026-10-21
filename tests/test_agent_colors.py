"""A teammate's name wears the color Claude Code gives it, as Claude Code draws it."""
import main
from collector import Agent


def test_teammate_names_wear_claude_codes_colors():
    assert main.agent_name_style(Agent(agent_id="a", name="a", color="pink")) == "bold #c46686"
    assert main.agent_name_style(Agent(agent_id="a", name="a", color="blue")) == "bold #6a9bcc"
    assert main.agent_name_style(Agent(agent_id="a", name="a", color="")) == "bold"
    # the meta file is another program's: anything but a known color name stays plain
    assert main.agent_name_style(Agent(agent_id="a", name="a", color="link https://x.test")) == "bold"
