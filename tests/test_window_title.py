"""The terminal title (the Mac app's title bar, iTerm2's tab) carries the farm emoji, the
version and one chicken per live session that is not idle. Textual never sets a terminal title, so the app
writes OSC 0 itself: on mount, when the count changes, and cleared on exit."""
import asyncio
import re
from pathlib import Path

from textual.drivers.headless_driver import HeadlessDriver

import main
from collector import Instance


def inst(pid, status, alive=True):
    return Instance(pid=pid, session_id=f"s{pid}", name=f"n{pid}", alive=alive, status=status)


def test_title_sequence_is_osc_zero():
    assert main.title_sequence("🌾 agent-farm") == "\x1b]0;🌾 agent-farm\x07"
    assert main.title_sequence("") == "\x1b]0;\x07"


def test_control_characters_never_reach_the_terminal_inside_a_title():
    # BEL ends the title early, ESC starts a new command (here: clear the screen), and the
    # 8-bit ST and CSI do the same in terminals that read C1 controls.
    seq = main.title_sequence("🌾 agent-farm v1\x07\x1b[2J\x9c\x9b2J\x7f\n.2 🐓")
    assert seq == "\x1b]0;🌾 agent-farm v1[2J2J.2 🐓\x07"
    body = seq[len("\x1b]0;"):-1]
    assert not any(ord(c) < 0x20 or 0x7F <= ord(c) <= 0x9F for c in body)


def test_chickens_count_live_sessions_that_are_not_idle(monkeypatch):
    monkeypatch.setattr(main, "WINDOW_TITLE", "🌾 agent-farm v0.9.1")
    assert main.window_title([]) == "🌾 agent-farm v0.9.1"
    assert main.window_title([inst(1, "idle")]) == "🌾 agent-farm v0.9.1"
    farm = [inst(1, "busy"), inst(2, "waiting"), inst(3, "idle"), inst(4, "busy", alive=False),
            inst(5, "shell")]                  # idle while its own command runs: drawn as idle
    assert main.window_title(farm) == "🌾 agent-farm v0.9.1 🐓🐓"


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
            assert writes[1:] == [main.title_sequence(f"{main.WINDOW_TITLE} 🐓")]
            await pilot.pause(2.5)                           # more ticks, same count: no rewrite
            assert len(writes) == 2
            await pilot.press("q")

    asyncio.run(go())
    assert writes[-1] == main.title_sequence("")


APP_DELEGATE = Path(__file__).resolve().parent.parent / "macos" / "Sources" / "AgentFarm" / "AppDelegate.swift"


def swift_default_title() -> tuple[str, str]:
    """The Mac app's default title as (text before the version, title without a version),
    read from its one definition in AppDelegate.swift."""
    m = re.search(r'private let defaultTitle = AppDelegate\.version\.map \{ "(.*)\\\(\$0\)" \} \?\? "(.*)"',
                  APP_DELEGATE.read_text())
    assert m, "defaultTitle's definition in AppDelegate.swift changed shape; update this test with it"
    return m.group(1), m.group(2)


def test_the_title_names_the_version_or_leaves_it_out():
    assert main.title_for("0.9.1") == "🌾 agent-farm v0.9.1"
    assert main.title_for("") == "🌾 agent-farm"         # no version known: no stray "v"
    assert main.WINDOW_TITLE == main.title_for(main.VERSION)


def test_the_mac_apps_default_title_reads_like_the_monitors():
    # The window shows the Swift default until the monitor sets its title and after it stops,
    # so the two must agree or the title changes at every launch and quit.
    before_version, without_version = swift_default_title()
    assert main.title_for("0.9.1") == f"{before_version}0.9.1"
    assert main.title_for("") == without_version
