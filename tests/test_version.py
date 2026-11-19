"""The window title shows the release number: the Mac app passes it in AGENT_FARM_VERSION
and a run from a checkout reads the VERSION file. There is no header bar inside the window."""
import asyncio

import apppaths
import main


def test_version_prefers_the_app_environment(monkeypatch):
    monkeypatch.setenv("AGENT_FARM_VERSION", "0.9.1")
    assert apppaths.app_version() == "0.9.1"
    monkeypatch.setenv("AGENT_FARM_VERSION", "v0.9.2")
    assert apppaths.app_version() == "0.9.2"


def test_version_falls_back_to_the_version_file(monkeypatch, tmp_path):
    monkeypatch.delenv("AGENT_FARM_VERSION", raising=False)
    (tmp_path / "VERSION").write_text("1.2.3\n")
    monkeypatch.setattr(apppaths, "CODE_DIR", tmp_path)
    assert apppaths.app_version() == "1.2.3"
    (tmp_path / "VERSION").unlink()
    assert apppaths.app_version() == ""


def test_the_title_carries_the_version():
    v = apppaths.app_version()
    assert v                                   # a checkout has a VERSION file
    assert main.WINDOW_TITLE == f"🌾 agent-farm v{v}"


def test_no_header_bar_inside_the_window():
    async def go():
        app = main.AgentFarm()
        app.collector.snapshot = lambda: []
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.2)
            assert not app.screen.query("Header")
            assert app.query_one("#body").region.y == 0     # the columns start on the top row
            await pilot.press("s")
            await pilot.pause(0.2)
            assert type(app.screen).__name__ == "SettingsScreen"
            assert not app.screen.query("Header")
            await pilot.press("escape")
            await pilot.press("q")

    asyncio.run(go())
