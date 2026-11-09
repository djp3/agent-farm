"""The line between the columns can be dragged. It sets the usage column's width, which is
remembered, and it never squeezes the session list or the panel's text past their least."""
import asyncio

import main
import usage
from expiring import ExpiryScanner
from settings import Settings


def _run(tmp_path, monkeypatch, steps, settings=None, size=(120, 30)):
    monkeypatch.setenv("AGENT_FARM_SETTINGS", str(tmp_path / "settings.json"))   # never the real one
    monkeypatch.setattr(usage, "PROJECTS_DIR", tmp_path / "claude-projects")
    (tmp_path / "claude" / "projects").mkdir(parents=True)

    async def go():
        app = main.AgentFarm(show_usage=True, settings=settings or Settings())
        app.collector.snapshot = lambda: []
        app.expiry = ExpiryScanner(tmp_path / "claude")
        app.limits.refresh = lambda force=False: False
        async with app.run_test(size=size) as pilot:
            await pilot.pause(0.5)
            await steps(app, pilot)
            await pilot.press("q")
    asyncio.run(go())


def test_dragging_the_divider_sets_the_usage_columns_width_and_remembers_it(tmp_path, monkeypatch):
    async def steps(app, pilot):
        col, divider = app.query_one("#usage"), app.query_one("#divider")
        assert col.outer_size.width == Settings().usage_width == 49
        assert (divider.region.x, divider.region.width) == (120 - 50, 1)   # where the old border was
        assert app._usage_text_width() == 47
        await pilot.mouse_down(divider, button=3)                          # only the left button drags
        assert not divider.has_class("-dragging")
        await pilot.mouse_down(divider)
        assert divider.has_class("-dragging")
        await pilot.hover(None, offset=(60, 10))
        await pilot.pause(0.2)
        assert col.outer_size.width == 59 and divider.region.x == 60
        assert app._usage_text_width() == 57 and app._usage_width == 59    # the text was drawn again
        assert not (tmp_path / "settings.json").exists()                  # kept once the button is up
        await pilot.mouse_up(None, offset=(60, 10))
        await pilot.pause(0.2)
        assert not divider.has_class("-dragging") and app.mouse_captured is None
        assert Settings.load().usage_width == 59
        await pilot.hover(None, offset=(80, 10))                           # the drag is over
        await pilot.pause(0.2)
        assert col.outer_size.width == 59
    _run(tmp_path, monkeypatch, steps)


def test_a_drag_stops_at_the_session_lists_room_and_the_panels_narrowest(tmp_path, monkeypatch):
    async def steps(app, pilot):
        col, divider = app.query_one("#usage"), app.query_one("#divider")
        await pilot.mouse_down(divider)
        await pilot.hover(None, offset=(5, 10))
        await pilot.pause(0.2)
        assert col.outer_size.width == 120 - 1 - main.LEFT_MIN_W
        assert app.query_one("#left").outer_size.width == main.LEFT_MIN_W
        await pilot.hover(None, offset=(119, 10))
        await pilot.pause(0.2)
        assert col.outer_size.width == main.USAGE_MIN_W and app._usage_text_width() == 30
        await pilot.mouse_up(None, offset=(119, 10))
        await pilot.pause(0.2)
        assert Settings.load().usage_width == main.USAGE_MIN_W
    _run(tmp_path, monkeypatch, steps)


def test_a_narrower_window_holds_the_column_back_without_forgetting_its_width(tmp_path, monkeypatch):
    async def steps(app, pilot):
        col = app.query_one("#usage")
        assert col.outer_size.width == 70
        await pilot.resize_terminal(90, 30)
        await pilot.pause(0.3)
        assert col.outer_size.width == 90 - 1 - main.LEFT_MIN_W and app.settings.usage_width == 70
        await pilot.resize_terminal(120, 30)
        await pilot.pause(0.3)
        assert col.outer_size.width == 70
    _run(tmp_path, monkeypatch, steps, Settings(usage_width=70))


def test_the_divider_goes_with_the_panel(tmp_path, monkeypatch):
    async def steps(app, pilot):
        divider = app.query_one("#divider")
        assert divider.display
        await pilot.press("u")
        await pilot.pause(0.2)
        assert not divider.display and not app.query_one("#usage").display
        assert app.query_one("#left").outer_size.width == 120
        await pilot.press("p")
        await pilot.pause(0.2)
        assert divider.display
    _run(tmp_path, monkeypatch, steps)


def test_the_pointer_is_the_resize_arrows_over_the_divider_and_all_through_a_drag(tmp_path, monkeypatch):
    async def steps(app, pilot):
        sent = []
        real = app._set_pointer_shape
        app._set_pointer_shape = lambda shape: (sent.append(shape), real(shape))
        divider = app.query_one("#divider")
        await pilot.hover(divider)
        assert sent[-1:] == ["ew-resize"]
        await pilot.hover("#tree")
        assert sent[-1:] == ["default"]
        await pilot.mouse_down(divider)
        await pilot.hover(None, offset=(5, 10))                # held at the list's room, past the line
        await pilot.pause(0.2)
        assert sent[-1:] == ["ew-resize"] and not app.screen.selections     # and no text was selected
        await pilot.mouse_up(None, offset=(5, 10))
        await pilot.hover(None, offset=(6, 10))
        assert sent[-1:] == ["default"]
    _run(tmp_path, monkeypatch, steps)
