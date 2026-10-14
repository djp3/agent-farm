"""The header shows the release number: the Mac app passes it in AGENT_FARM_VERSION and a
run from a checkout reads the VERSION file."""
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


def test_header_shows_the_version():
    expected = f"v{apppaths.app_version()}"

    async def go():
        app = main.AgentFarm()
        app.collector.snapshot = lambda: []
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.2)
            assert app.sub_title == expected
            assert expected in str(app.screen.query_one("HeaderTitle").render())
            await pilot.press("q")

    asyncio.run(go())
