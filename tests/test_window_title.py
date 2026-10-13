"""The terminal title (the Mac app's title bar, iTerm2's tab) carries the farm emoji and one
chicken per live session that is not idle. Textual never sets a terminal title, so the app
writes OSC 0 itself: on mount, when the count changes, and cleared on exit."""
import asyncio

from textual.drivers.headless_driver import HeadlessDriver

import main
from collector import Instance


def inst(pid, status, alive=True):
    return Instance(pid=pid, session_id=f"s{pid}", name=f"n{pid}", alive=alive, status=status)


def test_title_sequence_is_osc_zero():
    assert main.title_sequence("🌾 agent-farm") == "\x1b]0;🌾 agent-farm\x07"
    assert main.title_sequence("") == "\x1b]0;\x07"


def test_chickens_count_live_sessions_that_are_not_idle():
    assert main.window_title([]) == "🌾 agent-farm"
    assert main.window_title([inst(1, "idle")]) == "🌾 agent-farm"
    farm = [inst(1, "busy"), inst(2, "waiting"), inst(3, "idle"), inst(4, "busy", alive=False)]
    assert main.window_title(farm) == "🌾 agent-farm 🐓🐓"


def test_app_sets_title_on_mount_updates_it_and_clears_it_on_quit(monkeypatch):
    writes = []
    # Textual writes other escapes too (OSC 22 cursor shapes); keep only title sequences.
    monkeypatch.setattr(HeadlessDriver, "write",
                        lambda self, data: writes.append(data) if data.startswith("\x1b]0;") else None)
    farm = [inst(1, "busy"), inst(2, "idle")]

    async def go():
        app = main.AgentFarm()
        app.collector.snapshot = lambda: farm
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.2)
            assert writes[0] == main.title_sequence(main.WINDOW_TITLE)
            for _ in range(30):                              # a tick applies the snapshot
                await pilot.pause(0.1)
                if len(writes) > 1:
                    break
            assert writes[1:] == [main.title_sequence("🌾 agent-farm 🐓")]
            await pilot.pause(2.5)                           # more ticks, same count: no rewrite
            assert len(writes) == 2
            await pilot.press("q")

    asyncio.run(go())
    assert writes[-1] == main.title_sequence("")
