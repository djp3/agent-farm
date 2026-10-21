"""The selected row keeps the colors that say its state: the highlight is a navy bar behind
the label, never a text color of its own (Textual's default repaints the label near-white)."""
import asyncio

import main
from collector import Instance, TranscriptState


def test_a_selected_waiting_row_keeps_its_yellow_summary_on_a_navy_bar():
    waiting = Instance(pid=1, session_id="s1", name="asks-you", alive=True, status="waiting",
                       status_detail="AskUserQuestion: Which option?")
    waiting.transcript = TranscriptState(path=None)
    idle = Instance(pid=2, session_id="s2", name="other", alive=True, status="idle")
    seen = {}

    async def go():
        app = main.AgentFarm()
        app.collector.snapshot = lambda: [waiting, idle]
        async with app.run_test(size=(100, 12)) as pilot:
            await pilot.pause(1.5)
            tree = app.query_one("#tree")
            tree.move_cursor(app.nodes["i:1"])
            await pilot.pause(0.3)
            for seg in tree.render_line(0):
                if seg.text.strip() and seg.style and seg.style.color:
                    seen[seg.text.strip()[:10]] = (seg.style.color.name, seg.style.bgcolor and seg.style.bgcolor.name)
            await pilot.press("q")

    asyncio.run(go())
    assert seen["asks-you"] == (main.WHITE, "#1f3b5c")
    assert seen["AskUserQue"] == (main.YELLOW, "#1f3b5c")
