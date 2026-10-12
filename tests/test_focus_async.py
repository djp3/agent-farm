"""The f key talks to iTerm2, which can stall for seconds; that must happen off the
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
        inst = Instance(pid=77, session_id="s77", name="payable", alive=True, status="idle")
        inst.iterm_session, inst.iterm_title = "ABC", "payable tab"
        app.collector.snapshot = lambda: [inst]
        app.collector.focus_instance = fake_focus
        app.notify = lambda message, **kw: toasts.append(message)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(1.5)                      # a tick applies the one-row snapshot
            await pilot.press("f")
            for _ in range(20):                          # the worker finishes within a moment
                await pilot.pause(0.1)
                if toasts:
                    break
            await pilot.press("q")

    asyncio.run(go())
    assert calls == [(77, False)]                        # called once, not on the main thread
    assert toasts == ["focused payable tab"]
