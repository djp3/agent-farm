"""The o key (open) talks to iTerm2, which can stall for seconds; that must happen off the
thread that draws the screen, with the result posted back as a notification."""
import asyncio
import threading

import main
from collector import Instance


def test_focus_runs_off_the_ui_thread_and_notifies():
    calls = []
    toasts = []

    def fake_focus(inst, label=""):
        calls.append((inst.pid, threading.current_thread() is threading.main_thread()))
        return True, ""

    async def go():
        app = main.AgentFarm()
        inst = Instance(pid=77, session_id="s77", name="orchard", alive=True, status="idle")
        inst.iterm_session, inst.iterm_title = "ABC", "orchard tab"
        app.collector.snapshot = lambda: [inst]
        app.collector.focus_instance = fake_focus
        app.notify = lambda message, **kw: toasts.append(message)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(1.5)                      # a tick applies the one-row snapshot
            await pilot.press("o")
            for _ in range(20):                          # the worker finishes within a moment
                await pilot.pause(0.1)
                if toasts:
                    break
            await pilot.press("q")

    asyncio.run(go())
    assert calls == [(77, False)]                        # called once, not on the main thread
    assert toasts == ["focused orchard tab"]


def test_o_opens_a_session_and_f_no_longer_does():
    """o for open, as in the other agent-* apps (agent-frolic's o opens a pull request); the
    footer says so, and f, the key it replaced, does nothing now."""
    keys = {b.key: b for b in main.AgentFarm.BINDINGS}
    assert keys["o"].action == "focus_tab" and keys["o"].description == "Open" and keys["o"].show
    assert "f" not in keys
    calls = []

    async def go():
        app = main.AgentFarm()
        inst = Instance(pid=78, session_id="s78", name="pasture", alive=True, status="idle")
        inst.iterm_session, inst.iterm_title = "DEF", "pasture tab"
        app.collector.snapshot = lambda: [inst]
        app.collector.focus_instance = lambda inst, label="": calls.append(inst.pid) or (True, "")
        app.notify = lambda message, **kw: None
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(1.5)
            await pilot.press("f")
            await pilot.pause(0.5)
            assert calls == []
            await pilot.press("o")
            for _ in range(20):
                await pilot.pause(0.1)
                if calls:
                    break
            await pilot.press("q")

    asyncio.run(go())
    assert calls == [78]
