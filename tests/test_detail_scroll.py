"""The detail pane scrolls when the selected row has more to say than fits: the wheel moves
it, the refresh every tick keeps the place, and choosing another row starts at its top."""
import asyncio
from pathlib import Path

from textual import events

import main
from collector import Instance, TranscriptState


def _wheel_down() -> events.MouseScrollDown:
    return events.MouseScrollDown(None, 1, 1, 0, 1, 0, False, False, False)


def _session(pid: int, name: str, message: str) -> Instance:
    inst = Instance(pid=pid, session_id=f"s{pid}", name=name, alive=True, status="idle")
    inst.transcript = TranscriptState(path=Path(f"/nonexistent/s{pid}.jsonl"))
    inst.transcript.last_text = message
    return inst


def test_the_detail_pane_scrolls_and_starts_each_row_at_its_top():
    long = _session(71, "orchard", "\n".join(f"row {n} of the last message" for n in range(50)))
    short = _session(72, "pasture", "done")

    async def go():
        app = main.AgentFarm()
        app.collector.snapshot = lambda: [long, short]
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(1.5)                      # a tick applies the snapshot
            tree, pane = app.query_one("#tree"), app.query_one("#detail")
            body = app.query_one("#detail-body")
            row = {app.objects[k].name: node for k, node in app.nodes.items() if k in app.objects
                   and isinstance(app.objects[k], Instance)}
            tree.move_cursor(row["orchard"])
            await pilot.pause(0.2)
            assert "row 49 of the last message" in str(body.render())
            assert pane.max_scroll_y > 0 and pane.scroll_y == 0           # more than fits, at the top
            for _ in range(3):
                body.post_message(_wheel_down())                         # the wheel, over the text
            await pilot.pause(0.2)
            scrolled = pane.scroll_y
            assert scrolled > 0
            assert tree.has_focus                                        # focus stays with the list
            app.collect()                                                # the next tick redraws it
            await pilot.pause(1.0)
            assert pane.scroll_y == scrolled
            tree.move_cursor(row["pasture"])                             # another row: its top
            await pilot.pause(0.2)
            assert pane.scroll_y == 0 and "done" in str(body.render())
            tree.move_cursor(row["orchard"])                             # and back: its top again
            await pilot.pause(0.2)
            assert pane.scroll_y == 0
            await pilot.press("q")

    asyncio.run(go())
