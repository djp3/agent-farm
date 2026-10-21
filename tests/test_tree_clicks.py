"""Only the triangle folds a row under the mouse; a click on the name selects the row and
leaves it folded or unfolded as it was. Enter and space still fold from the keyboard."""
import asyncio

import main
from collector import Agent, Instance


def test_the_triangle_folds_and_the_name_only_selects():
    head = Instance(pid=1, session_id="s1", name="amethyst", alive=True, status="idle")
    head.agents = [Agent(agent_id="w", name="worker", status="dormant")]
    other = Instance(pid=2, session_id="s2", name="other", alive=True, status="idle")
    states = []

    async def go():
        app = main.AgentFarm()
        app.collector.snapshot = lambda: [head, other]
        async with app.run_test(size=(100, 14)) as pilot:
            await pilot.pause(1.5)
            tree = app.query_one("#tree")
            node = app.nodes["i:1"]
            tree.move_cursor(app.nodes["i:2"])
            await pilot.pause(0.2)
            states.append(node.is_expanded)                       # unfolded by default
            await pilot.click("#tree", offset=(6, 0))             # the name
            await pilot.pause(0.2)
            states.append((node.is_expanded, tree.cursor_node is node))
            await pilot.click("#tree", offset=(1, 0))             # the triangle
            await pilot.pause(0.2)
            states.append(node.is_expanded)
            await pilot.click("#tree", offset=(6, 0))             # the name again: stays folded
            await pilot.pause(0.2)
            states.append(node.is_expanded)
            await pilot.press("enter")
            await pilot.pause(0.2)
            states.append(node.is_expanded)
            await pilot.press("space")
            await pilot.pause(0.2)
            states.append(node.is_expanded)
            await pilot.press("q")

    asyncio.run(go())
    assert states == [True, (True, True), False, False, True, False]
