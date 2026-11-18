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


# ---- releasing connections (2026-11-18: 509 leaked connections hung iTerm2) ----------------- #
class FakeTransport:
    def __init__(self):
        self.aborted = False

    def abort(self):
        self.aborted = True


class ClosingWebsocket(FakeWebsocket):
    def __init__(self, close_hangs=False):
        super().__init__()
        self.close_hangs, self.close_calls, self.transport = close_hangs, 0, FakeTransport()

    async def close(self):
        self.close_calls += 1
        if self.close_hangs:                                           # a stuck iTerm2 never answers
            await asyncio.Event().wait()
        self.closed.set()


class ReleasableConnection:
    """Shaped like iterm2.Connection: a websocket, and the library's reader as a task."""
    def __init__(self, close_hangs=False):
        self.websocket = ClosingWebsocket(close_hangs)
        self._Connection__dispatch_forever_future = asyncio.ensure_future(asyncio.Event().wait())

    @property
    def released(self):
        return self.websocket.transport.aborted and self._Connection__dispatch_forever_future.done()


class Stop(Exception):
    pass


def _drive(monkeypatch, m, follow, cycles):
    """Run _follow_forever with iTerm2 running for `cycles` connections, each followed by
    `follow`. Returns the waits between them, the connections, and how many were open at once
    when each was made."""
    waits, made, open_at_connect = [], [], []
    monkeypatch.setattr(collector, "iterm_running", lambda: True)

    async def connect():
        open_at_connect.append(sum(not c.released for c in made))
        made.append(ReleasableConnection())
        return made[-1]
    monkeypatch.setattr(m, "_connect", connect)
    monkeypatch.setattr(m, "_follow", follow)

    async def sleep(seconds):
        waits.append(seconds)
        if len(waits) >= cycles:
            raise Stop
    monkeypatch.setattr(m, "_sleep", sleep)
    with pytest.raises(Stop):
        asyncio.run(m._follow_forever())
    return waits, made, open_at_connect


async def _closed_at_once(_connection):
    from websockets.exceptions import ConnectionClosedError
    raise ConnectionClosedError(None, None)                           # what every reconnect hit


def test_a_connection_that_fails_is_closed_and_cut(monkeypatch):
    m = ITerm2Map()
    _, made, _ = _drive(monkeypatch, m, _closed_at_once, 1)
    (connection,) = made
    assert connection.websocket.close_calls == 1                       # a closing handshake first
    assert connection.websocket.transport.aborted                      # then the socket, regardless
    assert connection._Connection__dispatch_forever_future.cancelled()  # and the library's reader
    assert not m.connected and m.state == "starting" and m._app is None


def test_a_close_that_never_finishes_still_cuts_the_socket(monkeypatch):
    monkeypatch.setattr(collector, "ITERM_CLOSE_S", 0.05)

    async def scenario():
        connection = ReleasableConnection(close_hangs=True)
        await asyncio.wait_for(ITerm2Map()._release(connection), 1)
        return connection
    connection = asyncio.run(scenario())
    assert connection.websocket.close_calls == 1 and connection.released


def test_a_release_cancelled_while_the_reader_stops_still_cuts_the_socket():
    async def slow_reader():
        while True:                                                    # shrugs off the first cancel
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await asyncio.sleep(10)

    async def scenario():
        connection = ReleasableConnection()
        connection._Connection__dispatch_forever_future.cancel()
        reader = connection._Connection__dispatch_forever_future = asyncio.ensure_future(slow_reader())
        await asyncio.sleep(0)
        release = asyncio.ensure_future(ITerm2Map()._release(connection))
        await asyncio.sleep(0.05)                                      # waiting on the reader now
        release.cancel()
        with pytest.raises(asyncio.CancelledError):
            await release
        reader.cancel()
        return connection
    connection = asyncio.run(scenario())
    assert connection.websocket.close_calls == 0                       # no time for a goodbye...
    assert connection.websocket.transport.aborted                      # ...but the socket is cut


def test_failures_back_off_instead_of_reconnecting_every_second(monkeypatch):
    waits, made, open_at_connect = _drive(monkeypatch, ITerm2Map(), _closed_at_once, 10)
    assert waits == [1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 60.0, 60.0, 60.0, 60.0]
    assert all(c.released for c in made)
    assert max(open_at_connect) == 0                                   # never more than the one in use


def test_only_a_connection_that_settled_resets_the_wait(monkeypatch):
    app = FakeApp(FakeWindow(FakeTab(FakeSession("A", "/dev/ttys001"))))
    m = ITerm2Map()
    plan = iter(["drop", "drop", "read", "drop", "read+steady", "drop"])

    async def follow(_connection):
        step = next(plan)
        if step.startswith("read"):
            await m._read(app)                                         # got as far as reading the map
        if step == "read+steady":
            monkeypatch.setattr(collector, "ITERM_STEADY_S", 0.0)      # ...and lasted long enough
        await _closed_at_once(_connection)
    waits, _, _ = _drive(monkeypatch, m, follow, 6)
    # a read alone doesn't settle it (iTerm2 can drop a connection right after); a read that lasted does
    assert waits == [1.0, 2.0, 4.0, 8.0, 1.0, 2.0]


def test_a_connection_made_after_the_timeout_is_released(monkeypatch):
    monkeypatch.setattr(collector.iterm2_auth, "applescript_auth_disabled", lambda: True)
    monkeypatch.setattr(collector, "ITERM_CONNECT_S", 0.05)
    made = []

    async def late():
        made.append(ReleasableConnection())
        try:
            await asyncio.sleep(0.2)
        except asyncio.CancelledError:
            pass                                                       # the handshake finished anyway
        return made[0]
    monkeypatch.setattr(collector.iterm2.Connection, "async_create", staticmethod(late))

    async def scenario():
        with pytest.raises(TimeoutError):
            await ITerm2Map()._connect()
        await asyncio.sleep(0.1)
    asyncio.run(scenario())
    assert made[0].released


def test_a_new_connection_gets_a_new_app_not_the_old_one_over_a_dead_connection(monkeypatch):
    """The root cause of the instant failures: iterm2.async_get_app keeps one App for the process
    and refreshes it over the connection it was made with, which died with the old iTerm2."""
    from websockets.exceptions import ConnectionClosedError

    class OldApp:
        async def async_refresh(self):
            raise ConnectionClosedError(None, None)
    monkeypatch.setattr(collector.iterm2.App, "instance", OldApp())
    fresh = FakeApp()

    async def construct(connection):
        return fresh
    monkeypatch.setattr(collector.iterm2.App, "async_construct", staticmethod(construct))
    assert asyncio.run(ITerm2Map._app_for("new connection")) is fresh


def test_releasing_forgets_the_subscriptions_and_the_app(monkeypatch):
    handlers = collector.iterm2_notifications._handlers
    mine, theirs, apps = object(), object(), object()
    monkeypatch.setitem(handlers, ("s1", 1), [mine, theirs])
    monkeypatch.setitem(handlers, ("s2", 2), [apps])

    class App:
        tokens = [(("s2", 2), apps)]
    monkeypatch.setattr(collector.iterm2.App, "instance", App)
    m = ITerm2Map()
    m._tokens = [(("s1", 1), mine), None]                              # a fake subscription returns None
    m._forget(App())
    assert handlers[("s1", 1)] == [theirs] and ("s2", 2) not in handlers
    assert collector.iterm2.App.instance is None and m._tokens == []


def test_the_one_shot_read_releases_its_connection(monkeypatch):
    app = FakeApp(FakeWindow(FakeTab(FakeSession("A", "/dev/ttys001"))))
    made = []
    monkeypatch.setattr(collector, "iterm_running", lambda: True)

    async def connect():
        made.append(ReleasableConnection())
        return made[-1]

    async def get_app(_connection):
        return app
    m = ITerm2Map()
    monkeypatch.setattr(m, "_connect", connect)
    monkeypatch.setattr(collector.iterm2, "async_get_app", get_app)
    m.refresh_once()
    assert set(m.by_tty) == {"/dev/ttys001"} and made[0].released


def test_many_instant_failures_leave_at_most_one_connection_open_to_iterm2(monkeypatch):
    """The 2026-11-18 hang against a real websocket server on a unix socket, where the library
    looks for iTerm2's, and the library's own connection code: every connection fails at once,
    as each one did after iTerm2 crashed, and none of them may stay open."""
    import shutil
    import tempfile
    from websockets.asyncio.server import unix_serve

    home = tempfile.mkdtemp(prefix="af-", dir="/tmp")                  # a unix socket path must be short
    try:
        folder = os.path.join(home, "Library", "Application Support", "iTerm2", "private")
        os.makedirs(folder)
        monkeypatch.setenv("HOME", home)
        monkeypatch.delenv("IT2_SUITE", raising=False)
        monkeypatch.setenv("ITERM2_COOKIE", "test")
        monkeypatch.setenv("ITERM2_KEY", "test")
        monkeypatch.setattr(collector.iterm2_auth, "applescript_auth_disabled", lambda: True)   # no AppleScript

        def no_tcp(self):
            raise AssertionError("never the real iTerm2's port")
        monkeypatch.setattr(collector.iterm2.Connection, "_get_tcp_connect_coro", no_tcp)
        monkeypatch.setattr(collector, "iterm_running", lambda: True)
        _follow_with(monkeypatch, None)
        monkeypatch.setattr(collector.iterm2, "async_get_app", _closed_at_once)
        m, cycles, now_open, most = ITerm2Map(), [], [0], [0]

        async def sleep(_seconds):
            cycles.append(_seconds)
            if len(cycles) >= 25:
                raise Stop
            await asyncio.sleep(0.02)                                  # time for the server to see a close
        monkeypatch.setattr(m, "_sleep", sleep)

        async def handler(ws):
            now_open[0] += 1
            most[0] = max(most[0], now_open[0])
            try:
                await ws.wait_closed()
            finally:
                now_open[0] -= 1

        async def scenario():
            async with unix_serve(handler, os.path.join(folder, "socket"), subprotocols=["api.iterm2.com"]):
                with pytest.raises(Stop):
                    await m._follow_forever()
                await asyncio.sleep(0.1)
                assert now_open[0] == 0
        asyncio.run(scenario())
        assert len(cycles) == 25 and most[0] == 1
    finally:
        shutil.rmtree(home, ignore_errors=True)


def test_a_lost_connection_is_logged_with_where_it_was_raised_now_and_then(monkeypatch, caplog):
    monkeypatch.setattr(collector, "_debounce", collector._Debounce())
    with caplog.at_level("INFO", logger=collector.log.name):
        _drive(monkeypatch, ITerm2Map(), _closed_at_once, 3)
    lost = [r for r in caplog.records if "lost the connection" in r.getMessage()]
    assert len(lost) == 3 and lost[0].exc_info and not lost[1].exc_info and not lost[2].exc_info
    assert "_closed_at_once" in caplog.text                            # the traceback, once
    assert "trying again in 2s" in lost[1].getMessage()
