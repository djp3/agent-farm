"""The tab map comes over one connection to iTerm2's Python API: one pass per connection,
never while iTerm2 is not running, the pass out of the environment once connected, a re-read
on each announcement and once a minute, and focus over the connection when there is one."""
import asyncio
import os
import threading

import pytest

import collector
from collector import ITerm2Map


class FakeSession:
    def __init__(self, sid, tty, name="tab"):
        self.session_id, self.tty, self.name = sid, tty, name
        self.activated = None

    async def async_get_variable(self, name):
        return {"tty": self.tty, "name": self.name}[name]

    async def async_activate(self, select_tab=True, order_window_front=True):
        self.activated = (select_tab, order_window_front)


class FakeTab:
    def __init__(self, *sessions):
        self.sessions = list(sessions)


class FakeWindow:
    def __init__(self, *tabs):
        self.tabs = list(tabs)


class FakeApp:
    def __init__(self, *windows):
        self.terminal_windows = list(windows)
        self.refreshes = 0

    async def async_refresh(self):
        self.refreshes += 1

    def get_session_by_id(self, sid):
        return next((s for w in self.terminal_windows for t in w.tabs for s in t.sessions if s.session_id == sid), None)


def test_the_pass_is_fetched_once_under_agent_farms_name_and_leaves_the_environment(monkeypatch):
    monkeypatch.delenv("ITERM2_COOKIE", raising=False)
    asked, seen = [], {}
    monkeypatch.setattr(collector.iterm2_auth, "applescript_auth_disabled", lambda: False)
    monkeypatch.setattr(collector.iterm2_auth, "request_cookie_and_key",
                        lambda launch, name, runner: asked.append((launch, name)) or "COOKIE-SECRET KEY-SECRET")

    async def create():
        seen["cookie"] = os.environ.get("ITERM2_COOKIE")
        return "connection"
    monkeypatch.setattr(collector.iterm2.Connection, "async_create", staticmethod(create))
    assert asyncio.run(ITerm2Map()._connect()) == "connection"
    assert asked == [(True, "agent-farm")] and seen["cookie"] == "COOKIE-SECRET"
    assert "ITERM2_COOKIE" not in os.environ and "ITERM2_KEY" not in os.environ

    async def refused():
        raise ConnectionRefusedError("no")
    monkeypatch.setattr(collector.iterm2.Connection, "async_create", staticmethod(refused))
    with pytest.raises(ConnectionRefusedError):
        asyncio.run(ITerm2Map()._connect())
    assert "ITERM2_COOKIE" not in os.environ                        # gone on failure too


def test_the_librarys_exit_on_an_old_version_is_an_error_not_an_exit(monkeypatch):
    monkeypatch.setattr(collector.iterm2_auth, "applescript_auth_disabled", lambda: True)

    async def exits():
        raise SystemExit(1)
    monkeypatch.setattr(collector.iterm2.Connection, "async_create", staticmethod(exits))
    with pytest.raises(RuntimeError):
        asyncio.run(ITerm2Map()._connect())


def test_while_iterm2_is_not_running_nothing_is_asked_and_the_map_empties(monkeypatch):
    m = ITerm2Map()
    m.by_tty = {"/dev/ttys001": {"id": "old"}}
    monkeypatch.setattr(collector, "iterm_running", lambda: False)

    async def must_not_connect():
        raise AssertionError("no pass request or connection while iTerm2 is not running")
    monkeypatch.setattr(m, "_connect", must_not_connect)

    class Stop(Exception):
        pass

    async def stop(_s):
        raise Stop
    monkeypatch.setattr(collector.asyncio, "sleep", stop)
    with pytest.raises(Stop):
        asyncio.run(m._follow_forever())
    assert m.by_tty == {}


def test_the_map_covers_every_window_tab_and_session():
    app = FakeApp(FakeWindow(FakeTab(FakeSession("A", "/dev/ttys001", "one"), FakeSession("B", "/dev/ttys002")),
                             FakeTab(FakeSession("C", ""))),
                  FakeWindow(FakeTab(FakeSession("D", "/dev/ttys004", None))))
    m = ITerm2Map()
    asyncio.run(m._read(app))
    assert set(m.by_tty) == {"/dev/ttys001", "/dev/ttys002", "/dev/ttys004"}        # no tty, no entry
    assert m.lookup("ttys001") == {"id": "A", "tty": "/dev/ttys001", "name": "one"}
    assert m.lookup("/dev/ttys004")["name"] == ""
    assert m.lookup("") == {} and m.lookup("ttys009") == {}


class FakeWebsocket:
    def __init__(self):
        self.closed = asyncio.Event()

    async def wait_closed(self):
        await self.closed.wait()


class FakeConnection:
    def __init__(self):
        self.websocket = FakeWebsocket()


def _follow_with(monkeypatch, app, recheck_s=60.0):
    callbacks = []

    async def subscribe(_connection, callback):
        callbacks.append(callback)
    for name in ("async_subscribe_to_layout_change_notification", "async_subscribe_to_new_session_notification",
                 "async_subscribe_to_terminate_session_notification"):
        monkeypatch.setattr(collector, name, subscribe)

    async def get_app(_connection):
        return app
    monkeypatch.setattr(collector.iterm2, "async_get_app", get_app)
    monkeypatch.setattr(collector, "ITERM_SETTLE_S", 0.01)
    monkeypatch.setattr(collector, "ITERM_RECHECK_S", recheck_s)
    return callbacks


def test_an_announcement_rereads_the_map_and_a_closed_connection_ends_it(monkeypatch):
    tab = FakeTab(FakeSession("A", "/dev/ttys001"))
    app = FakeApp(FakeWindow(tab))
    callbacks = _follow_with(monkeypatch, app)
    m, connection = ITerm2Map(), FakeConnection()

    async def scenario():
        follow = asyncio.ensure_future(m._follow(connection))
        await asyncio.sleep(0.05)
        assert m.connected and set(m.by_tty) == {"/dev/ttys001"} and app.refreshes == 0
        tab.sessions.append(FakeSession("B", "/dev/ttys002"))          # a new tab...
        for callback in callbacks:                                     # ...is a burst of announcements
            await callback(connection, None)
        await asyncio.sleep(0.1)
        assert set(m.by_tty) == {"/dev/ttys001", "/dev/ttys002"} and app.refreshes == 1
        connection.websocket.closed.set()                              # iTerm2 quit
        await asyncio.wait_for(follow, 1)
    asyncio.run(scenario())
    assert len(callbacks) == 3                                         # layout, new session, ended session


def test_without_announcements_it_still_rereads_now_and_then(monkeypatch):
    app = FakeApp(FakeWindow(FakeTab(FakeSession("A", "/dev/ttys001"))))
    _follow_with(monkeypatch, app, recheck_s=0.02)
    m, connection = ITerm2Map(), FakeConnection()

    async def scenario():
        follow = asyncio.ensure_future(m._follow(connection))
        await asyncio.sleep(0.15)
        connection.websocket.closed.set()
        await asyncio.wait_for(follow, 1)
    asyncio.run(scenario())
    assert app.refreshes >= 3


def _loop_in_thread():
    loop = asyncio.new_event_loop()
    threading.Thread(target=loop.run_forever, daemon=True).start()
    return loop


def test_focus_goes_over_the_connection_after_activating_iterm2(monkeypatch):
    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        return collector.subprocess.CompletedProcess(cmd, 0, "", "")
    monkeypatch.setattr(collector.subprocess, "run", run)
    session = FakeSession("S-1", "/dev/ttys001")
    m = ITerm2Map()
    m.available, m.connected, m._app, m._loop = True, True, FakeApp(FakeWindow(FakeTab(session))), _loop_in_thread()
    try:
        assert m.focus("S-1", "label") == (True, "")
        assert calls == [[collector.OPEN, "-b", collector.ITERM_BUNDLE_ID]]       # no it2
        assert session.activated == (True, True)
        # a session the connection does not know falls back to it2
        monkeypatch.setattr(collector.os.path, "exists", lambda p: True)
        assert m.focus("S-gone", "label") == (True, "")
        assert calls[-1] == [collector.IT2, "session", "focus", "S-gone"]
    finally:
        m._loop.call_soon_threadsafe(m._loop.stop)


def test_focus_without_a_connection_or_it2_says_so(monkeypatch):
    monkeypatch.setattr(collector.subprocess, "run",
                        lambda cmd, **kw: collector.subprocess.CompletedProcess(cmd, 0, "", ""))
    monkeypatch.setattr(collector.os.path, "exists", lambda p: False)
    m = ITerm2Map()
    m.available = True
    assert m.focus("S-1", "label") == (False, "iTerm2 is not connected")


def test_iterm_running_reads_the_process_table(monkeypatch):
    class P:
        def __init__(self, name):
            self.info = {"name": name}
    monkeypatch.setattr(collector.psutil, "process_iter", lambda attrs: [P("Finder"), P("iTerm2")])
    assert collector.iterm_running()
    monkeypatch.setattr(collector.psutil, "process_iter", lambda attrs: [P("Finder"), P("iTermServer-3.7.2")])
    assert not collector.iterm_running()


def test_the_map_records_its_state_and_each_beat(monkeypatch):
    app = FakeApp(FakeWindow(FakeTab(FakeSession("A", "/dev/ttys001"))))
    callbacks = _follow_with(monkeypatch, app)
    m, connection = ITerm2Map(), FakeConnection()
    assert m.state == "starting" and m.beat_at == 0.0

    async def scenario():
        follow = asyncio.ensure_future(m._follow(connection))
        await asyncio.sleep(0.05)
        assert m.state == "connected" and m.connected_at > 0
        first = m.beat_at
        assert first > 0
        await callbacks[0](connection, None)
        await asyncio.sleep(0.1)
        assert m.beat_at > first                                       # an announcement is a beat
        connection.websocket.closed.set()
        await asyncio.wait_for(follow, 1)
    asyncio.run(scenario())
    for _ in range(3):
        m._failed(ConnectionRefusedError("no"))
    assert m.state == "refused"


def test_the_heart_beats_and_says_what_it_shows():
    import main
    assert main.iterm_heart("connected", 0.2) == (main.HEART, main.HEART_BEAT)
    assert main.iterm_heart("connected", 30.0) == (main.HEART, main.HEART_CALM)
    assert main.iterm_heart("not running", 0.0) == (main.HEART_HOLLOW, main.GRAY)
    assert main.iterm_heart("starting", 0.0) == (main.HEART_HOLLOW, main.GRAY)
    assert main.iterm_heart("refused", 0.0) == (main.HEART_HOLLOW, main.RED)
    line = main.fleet_line([], False, 0, True, main.iterm_heart("connected", 0.2))
    assert line.plain.startswith("♥ ") and line.spans[0].style == main.HEART_BEAT
    assert not main.fleet_line([], False, 0, True).plain.startswith("♥")
    assert "connected for 2m00s, 14 sessions" in main.iterm_heart_words("connected", 100.0, 14, 220.0)
    assert "Enable Python API" in main.iterm_heart_words("refused", 0, 0, 1.0)
    assert "not running" in main.iterm_heart_words("not running", 0, 0, 1.0)
    tip = main.fleet_tooltip([], main.get_ordering(main.DEFAULT_ORDERING), 0, True, "♡ connecting to iTerm2…")
    assert "♡ connecting to iTerm2…" in tip
