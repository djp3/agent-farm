"""The push to agent-grange: which entries go, what reaches the wire, when a push is due,
what each answer means, and the switch, the g key and the login field that drive it. Made-up
names only. The HTTP tests talk to a server of their own on a free port; nothing here
reaches the grange on this Mac (conftest's grange_pushes takes the app's pushes)."""
import asyncio
import json
import logging
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

import grange
import main
from collector import Instance
from settings import Settings

REAL_PUT = grange.put_farm          # conftest replaces the module's copy for every test
ON = [{"pattern": r"^proj-\d+", "enabled": True}, {"pattern": "review", "enabled": False}]


def session(name, status="busy"):
    return SimpleNamespace(name=name, status=status)


# ---- what goes -------------------------------------------------------------------

def test_a_push_carries_the_matching_sessions_in_order_with_exactly_three_fields():
    sessions = [session("proj-12 fix tests", "waiting"), session("review the docs"),
                session("proj-7 lunch", "dead"), session("scratch", "idle")]
    agents, matched = grange.report(sessions, ON, "octo-cat")
    assert agents == [{"owner": "octo-cat", "name": "proj-12 fix tests", "state": "waiting"},
                      {"owner": "octo-cat", "name": "proj-7 lunch", "state": "dead"}]
    assert matched == 2


def test_no_pattern_that_is_on_means_an_empty_list():
    assert grange.report([session("proj-1 a")], [{"pattern": "proj", "enabled": False}], "octo-cat") == ([], 0)


def test_a_long_name_is_cut_to_256_bytes_at_a_character_boundary():
    name = "proj-1 " + "é" * 300                 # two bytes each
    [agent], _ = grange.report([session(name)], ON, "octo-cat")
    cut = agent["name"].encode()
    assert len(cut) <= grange.FIELD_MAX and len(cut) >= grange.FIELD_MAX - 1
    assert name.startswith(agent["name"])


def test_a_name_that_is_not_valid_text_is_made_valid_and_still_goes(server):
    name = "proj-1 caf" + b"\xe9".decode("utf-8", "surrogateescape")    # from bytes that weren't UTF-8
    agents, _ = grange.report([session(name)], ON, "octo-cat")
    assert agents[0]["name"] == "proj-1 caf?"
    g = server()
    assert REAL_PUT(g.port, agents).kind == "accepted"
    assert json.loads(g.got[0][3].decode("utf-8")) == {"agents": agents}


def test_a_session_without_a_name_is_not_sent_even_when_a_pattern_matches_nothing():
    assert grange.report([session(""), session("proj-1")], [{"pattern": ".*", "enabled": True}], "o") == \
        ([{"owner": "o", "name": "proj-1", "state": "busy"}], 1)


def test_past_200_matches_the_rest_are_left_out_and_counted():
    agents, matched = grange.report([session(f"proj-{i}") for i in range(230)], ON, "octo-cat")
    assert len(agents) == grange.AGENTS_MAX and matched == 230
    assert agents[0]["name"] == "proj-0" and agents[-1]["name"] == "proj-199"


SLOW = r"(a+)+b"                    # backtracks catastrophically on a long run of a's


def test_a_slow_pattern_costs_its_time_once_per_name_not_every_snapshot(monkeypatch):
    monkeypatch.setattr(grange, "TIME_LIMIT_S", 0.05)
    rules = [{"pattern": SLOW, "enabled": True}, {"pattern": "^zebra", "enabled": True}]
    sessions = [session("a" * 40 + f"-{i}") for i in range(4)] + [session("zebra-1"), session("aab")]
    began = time.monotonic()
    agents, _ = grange.report(sessions, rules, "octo-cat", budget=10)
    assert time.monotonic() - began >= 4 * grange.TIME_LIMIT_S      # each slow name ran out once
    assert [a["name"] for a in agents] == ["zebra-1", "aab"]
    began = time.monotonic()
    for _ in range(5):                                              # the next snapshots
        assert grange.report(sessions, rules, "octo-cat", budget=10)[0] == agents
    assert time.monotonic() - began < grange.TIME_LIMIT_S           # nothing ran out again
    rules[1]["pattern"] = "^aa"                                     # an edited pattern is checked anew
    assert [a["name"] for a in grange.report(sessions, rules, "octo-cat")[0]] == \
        [s.name for s in sessions[:4]] + ["aab"]


def test_a_burst_of_new_names_is_checked_over_several_snapshots_instead_of_freezing(monkeypatch):
    monkeypatch.setattr(grange, "TIME_LIMIT_S", 0.05)
    rules = [{"pattern": SLOW, "enabled": True}, {"pattern": "^zebra", "enabled": True}]
    sessions = [session("a" * 40 + f"-{i}") for i in range(12)] + [session("zebra-1")]
    budget, calls, agents = 0.1, 0, None
    while agents != [{"owner": "octo-cat", "name": "zebra-1", "state": "busy"}]:
        began = time.monotonic()
        agents, _ = grange.report(sessions, rules, "octo-cat", budget=budget)
        assert time.monotonic() - began < budget + grange.TIME_LIMIT_S + 0.1   # one snapshot's share
        calls += 1
        assert calls < 20
    assert calls > 1                    # 12 slow names were more than one snapshot's share
    assert agents == grange.report(sessions, rules, "octo-cat", budget=0)[0]   # all remembered now

# ---- the owner from gh -----------------------------------------------------------------

def _hosts(tmp_path, text):
    d = tmp_path / "gh"
    d.mkdir(exist_ok=True)
    (d / "hosts.yml").write_text(text)


def test_the_owner_is_gh_s_login_on_github_com(tmp_path):
    assert grange.gh_login() == ""               # conftest's empty folder
    _hosts(tmp_path, "ghe.example.com:\n    user: someone-else\n"
                     "github.com:\n    oauth_token: not-for-us\n    git_protocol: https\n"
                     "    users:\n        octo-cat:\n    user: octo-cat\n")
    assert grange.gh_login() == "octo-cat"
    _hosts(tmp_path, 'github.com:\n  user: "octo-cat"\n')
    assert grange.gh_login() == "octo-cat"


def test_no_login_when_gh_has_none_for_github_com_or_it_is_not_a_login(tmp_path):
    _hosts(tmp_path, "ghe.example.com:\n    user: someone-else\n")
    assert grange.gh_login() == ""
    _hosts(tmp_path, "github.com:\n    user: -not-a-login-\n")
    assert grange.gh_login() == ""


def test_an_enterprise_managed_users_login_is_a_login(tmp_path):
    """Their logins are handle_shortcode: refused, their sessions were never reported."""
    for login in ("mira_acme", "mira-dev_acme", "octo-cat", "a", "a" * 39):
        assert grange.LOGIN.fullmatch(login), login
    for wrong in ("_mira", "mira_", "mira__acme", "mira-_acme", "mira acme", "mira.acme", "a" * 40, ""):
        assert not grange.LOGIN.fullmatch(wrong), wrong
    _hosts(tmp_path, "github.com:\n    user: mira_acme\n")
    assert grange.gh_login() == "mira_acme"


def test_an_enterprise_managed_users_login_is_kept_in_the_settings(tmp_path, monkeypatch):
    p = tmp_path / "settings.json"
    monkeypatch.setenv("AGENT_FARM_SETTINGS", str(p))
    p.write_text(json.dumps({"grange_owner": "mira_acme"}))
    assert Settings.load().grange_owner == "mira_acme"


# ---- the wire ------------------------------------------------------------------------

class Grange:
    """A grange of our own on a free port: answers each PUT with `code` and `body` and keeps
    what it was sent."""

    def __init__(self, code=204, body=b"", headers=()):
        self.got = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_PUT(self):
                n = int(self.headers.get("Content-Length", 0))
                outer.got.append((self.command, self.path, dict(self.headers), self.rfile.read(n)))
                self.send_response(code)
                for k, v in headers:
                    self.send_header(k, v)
                if body:
                    self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def server():
    made = []

    def make(*args, **kwargs):
        made.append(Grange(*args, **kwargs))
        return made[-1]

    yield make
    for g in made:
        g.close()


def test_a_push_is_a_put_of_the_whole_list_with_a_content_length(server):
    g = server()
    agents = [{"owner": "octo-cat", "name": "proj-12 fix tests ☕", "state": "busy"}]
    assert REAL_PUT(g.port, agents) == grange.Reply(204)
    [(method, path, headers, body)] = g.got
    assert (method, path) == ("PUT", "/v1/farm")
    assert int(headers["Content-Length"]) == len(body) and "Transfer-Encoding" not in headers
    assert headers["Content-Type"] == "application/json"
    assert json.loads(body) == {"agents": agents}


def test_an_empty_list_is_a_push_too(server):
    g = server()
    assert REAL_PUT(g.port, []).kind == "accepted"
    assert json.loads(g.got[0][3]) == {"agents": []}


@pytest.mark.parametrize("code,error", [(400, "missing owner"), (413, "more than 200 agents")])
def test_a_refusal_says_what_the_grange_said(server, code, error):
    g = server(code, json.dumps({"error": error}).encode())
    reply = REAL_PUT(g.port, [])
    assert reply == grange.Reply(code, error) and reply.kind == "refused"


def test_an_out_of_date_grange_is_a_reply_to_wait_out(server):
    body = json.dumps({"error": "image out of date", "fix": "make update", "details": ["x"]}).encode()
    g = server(503, body, headers=[("Retry-After", "60"), ("Connection", "close")])
    reply = REAL_PUT(g.port, [])
    assert reply.code == 503 and reply.kind == "out of date"


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_no_grange_running_is_a_reply_not_an_error():
    reply = REAL_PUT(_free_port(), [])
    assert reply == grange.Reply(None, "not running") and reply.kind == "not running"


def test_a_grange_that_never_answers_is_given_up_on(monkeypatch):
    monkeypatch.setattr(grange, "TIMEOUT_S", 0.3)
    with socket.socket() as s:                   # accepts the connection, never answers
        s.bind(("127.0.0.1", 0))
        s.listen()
        reply = REAL_PUT(s.getsockname()[1], [])
    assert reply.code is None and reply.kind == "no answer" and "0.3" in reply.problem


# ---- when -----------------------------------------------------------------------------

A = [{"owner": "o", "name": "proj-1", "state": "busy"}, {"owner": "o", "name": "proj-2", "state": "idle"}]


def test_a_grange_that_answers_a_byte_at_a_time_is_given_up_on_in_the_time_for_one_push(monkeypatch):
    monkeypatch.setattr(grange, "TIMEOUT_S", 0.3)
    srv = socket.create_server(("127.0.0.1", 0))
    stop = threading.Event()

    def drip():
        c, _ = srv.accept()
        c.recv(65536)
        try:
            for ch in b"HTTP/1.1 204 No Content\r\nX-Slow: " + b"a" * 50 + b"\r\n\r\n":
                if stop.wait(0.05):     # each byte well inside the timeout for one wait
                    break
                c.sendall(bytes([ch]))
        except OSError:
            pass                        # the push hung up on it
        c.close()

    t = threading.Thread(target=drip, daemon=True)
    t.start()
    began = time.monotonic()
    reply = REAL_PUT(srv.getsockname()[1], [])
    took = time.monotonic() - began
    stop.set()
    t.join(5)
    srv.close()
    assert reply == grange.Reply(None, "no answer in 0.3 s")
    assert took < 1.0                   # the whole answer would take over 3 s


def test_a_push_goes_on_a_change_and_on_the_heartbeat_but_not_for_a_new_order():
    r = grange.Reporter()
    assert r.next_push(A, 0.0) == A
    r.done(grange.Reply(204), 0.1, 7788)
    assert r.next_push(A, 1.0) is None
    assert r.next_push(list(reversed(A)), 2.0) is None              # the grange ignores order
    changed = [A[0], dict(A[1], state="waiting")]
    assert r.next_push(changed, 3.0) == changed
    r.done(grange.Reply(204), 3.1, 7788)
    assert r.next_push(changed, 3.0 + grange.HEARTBEAT_S - 0.5) is None
    assert r.next_push(changed, 3.0 + grange.HEARTBEAT_S) == changed


def test_one_push_is_out_at_a_time_and_the_latest_list_follows_it():
    r = grange.Reporter()
    r.next_push(A, 0.0)
    assert r.next_push(A[:1], 1.0) is None                         # the first is still out
    r.done(grange.Reply(204), 1.5, 7788)
    assert r.next_push(A[:1], 2.0) == A[:1]


def test_a_grange_that_is_down_is_tried_on_the_heartbeat_not_every_second():
    r = grange.Reporter()
    r.next_push(A, 0.0)
    r.done(grange.Reply(None, "not running"), 0.0, 7788)
    assert all(r.next_push(A, t) is None for t in range(1, int(grange.HEARTBEAT_S)))
    assert r.next_push(A, grange.HEARTBEAT_S) == A


def test_turning_it_off_sends_one_empty_list_then_nothing():
    r = grange.Reporter()
    assert r.next_push(None, 0.0) is None                           # never on: nothing to clear
    r.next_push(A, 1.0)
    r.done(grange.Reply(204), 1.0, 7788)
    assert r.next_push(None, 2.0) == []
    r.done(grange.Reply(204), 2.0, 7788)
    assert r.next_push(None, 3.0) is None and r.next_push(None, 300.0) is None
    assert r.next_push(A, 301.0) == A and r.reply is None           # on again: a fresh start


def test_an_empty_list_while_on_is_pushed_and_then_needs_no_clearing():
    r = grange.Reporter()
    assert r.next_push([], 0.0) == []
    r.done(grange.Reply(204), 0.0, 7788)
    assert r.next_push(None, 1.0) is None


def test_the_status_says_what_happened(monkeypatch):
    r = grange.Reporter()
    assert r.words(False, "octo-cat", 7788, 0) == "Off: nothing is sent."
    assert "GitHub login" in r.words(True, "", 7788, 0)
    r.next_push(A, 0.0, matched=2)
    assert r.words(True, "octo-cat", 7788, 0) == "On: sending to port 7788…"
    r.done(grange.Reply(204), 10.0, 7788)
    assert r.words(True, "octo-cat", 7788, 10.5) == "On: 2 sessions sent to port 7788, accepted just now."
    assert "accepted 5 s ago" in r.words(True, "octo-cat", 7788, 15.0)
    r.next_push(A[:1], 16.0, matched=1)                 # on its way: the words still describe the
    assert r.words(True, "octo-cat", 7788, 16.0) == "On: 2 sessions sent to port 7788, accepted 6 s ago."
    r.done(grange.Reply(204), 16.5, 7788)               # last answer, until this one's comes
    assert r.words(True, "octo-cat", 7788, 17.0) == "On: 1 session sent to port 7788, accepted just now."
    for reply, said in [(grange.Reply(None, "not running"), "no agent-grange is running on port 7788"),
                        (grange.Reply(503, "image out of date"), "make update"),
                        (grange.Reply(400, "missing owner"), "refused the list (400: missing owner)"),
                        (grange.Reply(None, "no answer in 3 s"), "push to port 7788 failed (no answer in 3 s)")]:
        r.done(reply, 20.0, 7788)
        assert said in r.words(True, "octo-cat", 7788, 21.0)
    r.out = False
    r.next_push([dict(A[0], name=f"proj-{i}") for i in range(200)], 100.0, matched=230)
    r.done(grange.Reply(204), 100.0, 7788)
    assert "30 more match, past the grange's limit of 200" in r.words(True, "octo-cat", 7788, 100.0)


def test_the_log_notes_each_change_of_answer_once_and_never_a_session(caplog):
    r = grange.Reporter()
    secret = "proj-9 the-secret-plan"
    with caplog.at_level(logging.INFO, logger="agent_farm.grange"):
        for t in range(5):
            r.next_push([{"owner": "octo-cat", "name": secret, "state": "busy"}], t * 30.0)
            r.done(grange.Reply(None, "not running") if t < 3 else grange.Reply(204), t * 30.0, 7788)
    messages = [rec.getMessage() for rec in caplog.records]
    assert messages == ["no agent-grange on port 7788; trying every 20 s",
                        "agent-grange on port 7788 took the report"]
    assert not any(secret in m or "octo-cat" in m for m in messages)


# ---- the app --------------------------------------------------------------------------

def _instances():
    return [Instance(pid=101, session_id="s1", name="proj-12 fix tests", status="waiting"),
            Instance(pid=102, session_id="s2", name="scratch", status="busy"),
            Instance(pid=103, session_id="s3", name="proj-7 docs", status="idle")]


def _app(tmp_path, monkeypatch, **saved):
    p = tmp_path / "settings.json"
    monkeypatch.setenv("AGENT_FARM_SETTINGS", str(p))
    p.write_text(json.dumps({"grange_patterns": ON, **saved}))
    app = main.AgentFarm()
    app.collector.snapshot = _instances
    return app, p


def _run(app, steps):
    async def go():
        async with app.run_test(size=(110, 60)) as pilot:
            await pilot.pause(0.3)
            await steps(app, pilot)
            await pilot.press("q")
    asyncio.run(go())


EXPECTED = [{"owner": "octo-cat", "name": "proj-12 fix tests", "state": "waiting"},
            {"owner": "octo-cat", "name": "proj-7 docs", "state": "idle"}]


def test_with_reporting_on_the_app_pushes_the_matching_sessions_to_its_port(tmp_path, monkeypatch, grange_pushes):
    app, _ = _app(tmp_path, monkeypatch, grange_report=True, grange_owner="octo-cat", grange_port=7791)

    async def steps(app, pilot):
        assert grange_pushes[0] == (7791, EXPECTED)
        assert len(grange_pushes) == 1                   # snapshots with the same list don't push
        await pilot.pause(1.2)                           # the tooltip follows on the next snapshot
        assert "agent-grange: On: 2 sessions sent to port 7791" in app.query_one("#fleet").tooltip

    _run(app, steps)


def test_with_reporting_off_nothing_is_pushed(tmp_path, monkeypatch, grange_pushes):
    app, _ = _app(tmp_path, monkeypatch, grange_owner="octo-cat")

    async def steps(app, pilot):
        assert grange_pushes == []
        assert "agent-grange" not in app.query_one("#fleet").tooltip

    _run(app, steps)


def test_g_turns_reporting_off_and_on_and_saves_it(tmp_path, monkeypatch, grange_pushes):
    app, p = _app(tmp_path, monkeypatch, grange_report=True, grange_owner="octo-cat")

    async def steps(app, pilot):
        await pilot.press("g")
        await pilot.pause(0.3)
        assert json.loads(p.read_text())["grange_report"] is False
        assert grange_pushes[-1] == (grange.DEFAULT_PORT, [])         # the boards drop them now
        assert [n.message for n in app._notifications] == ["Stopped reporting to agent-grange."]
        await pilot.press("g")
        await pilot.pause(0.3)
        assert json.loads(p.read_text())["grange_report"] is True
        assert grange_pushes[-1] == (grange.DEFAULT_PORT, EXPECTED)
        assert [n.message for n in app._notifications][-1] == "Reporting 2 sessions to agent-grange."
        assert len(grange_pushes) == 3

    _run(app, steps)


def test_the_owner_comes_from_gh_when_the_settings_name_none(tmp_path, monkeypatch, grange_pushes):
    _hosts(tmp_path, "github.com:\n    user: octo-cat\n")
    app, _ = _app(tmp_path, monkeypatch, grange_report=True)

    async def steps(app, pilot):
        assert grange_pushes == [(grange.DEFAULT_PORT, EXPECTED)]

    _run(app, steps)


def test_without_a_login_nothing_is_pushed_and_the_status_says_why(tmp_path, monkeypatch, grange_pushes):
    app, _ = _app(tmp_path, monkeypatch, grange_report=True)

    async def steps(app, pilot):
        assert grange_pushes == []
        assert "nothing is sent until there is a GitHub login" in app.query_one("#fleet").tooltip

    _run(app, steps)


def _status(app):
    return str(app.screen.query_one("#grange-status").render())


def test_the_settings_switch_the_login_field_and_g_there(tmp_path, monkeypatch, grange_pushes):
    _hosts(tmp_path, "github.com:\n    user: octo-cat\n")
    app, p = _app(tmp_path, monkeypatch)

    async def steps(app, pilot):
        await pilot.press("s")
        await pilot.pause(0.2)
        switch, box = app.screen.query_one("#grange_report"), app.screen.query_one("#grange-owner")
        assert switch.value is False and box.value == "" and box.placeholder == "octo-cat, from gh"
        assert _status(app) == "Off: nothing is sent."
        await pilot.click("#grange_report")
        await pilot.pause(0.3)
        assert grange_pushes == [(grange.DEFAULT_PORT, EXPECTED)]
        assert _status(app) == "On: 2 sessions sent to port 7788, accepted just now."

        box.focus()
        await pilot.press(*"hubot")                       # typed, not yet the owner
        await pilot.pause(0.2)
        assert app.settings.grange_owner == "" and len(grange_pushes) == 1
        await pilot.press("enter")
        await pilot.pause(0.3)
        assert json.loads(p.read_text())["grange_owner"] == "hubot"
        assert grange_pushes[-1][1][0]["owner"] == "hubot"

        box.value = "not a login!"
        await pilot.press("enter")
        await pilot.pause(0.2)
        assert "is not a GitHub login" in _status(app) and app.settings.grange_owner == "hubot"
        box.value = ""                                     # back to gh's login
        await pilot.press("tab")                           # leaving the field saves it too
        await pilot.pause(0.3)
        assert app.settings.grange_owner == "" and grange_pushes[-1][1][0]["owner"] == "octo-cat"

        await pilot.press("g")                             # from a switch, not a field: g flips it
        await pilot.pause(0.3)
        assert switch.value is False and json.loads(p.read_text())["grange_report"] is False
        assert grange_pushes[-1] == (grange.DEFAULT_PORT, [])
        await pilot.press("escape")

    _run(app, steps)


class _Keep(logging.Handler):
    """What the app logs. The app's log stops at the agent_farm logger (setup_logging), so
    caplog, on the root logger, never sees it."""

    def __init__(self):
        super().__init__(logging.DEBUG)
        self.said = []

    def emit(self, record):
        self.said.append(record.getMessage())


def test_the_login_is_saved_but_never_written_to_the_log(tmp_path, monkeypatch):
    assert "hubot" not in repr(Settings(grange_owner="hubot"))
    app, p = _app(tmp_path, monkeypatch, grange_report=True)

    async def steps(app, pilot):
        await pilot.press("s")
        await pilot.pause(0.2)
        app.screen.query_one("#grange-owner").focus()
        await pilot.press(*"hubot", "enter")
        await pilot.pause(0.3)
        await pilot.click("#grange_report")                 # any later change logs the settings again
        await pilot.pause(0.3)
        await pilot.press("escape")

    keep, farm = _Keep(), logging.getLogger("agent_farm")
    farm.addHandler(keep)
    try:
        _run(app, steps)
    finally:
        farm.removeHandler(keep)
    assert json.loads(p.read_text())["grange_owner"] == "hubot"
    assert Settings.load().grange_owner == "hubot"
    said = keep.said
    assert sum("settings changed" in m for m in said) >= 2
    assert not any("hubot" in m for m in said)

def test_esc_in_the_middle_of_typing_a_login_closes_the_panel_cleanly(tmp_path, monkeypatch, grange_pushes):
    app, p = _app(tmp_path, monkeypatch, grange_report=True, grange_owner="octo-cat")

    async def steps(app, pilot):
        await pilot.press("s")
        await pilot.pause(0.2)
        box = app.screen.query_one("#grange-owner")
        box.focus()
        await pilot.press("backspace", "backspace", "backspace", "escape")
        await pilot.pause(0.3)
        assert not isinstance(app.screen, main.SettingsScreen) and app.is_running
        assert app.settings.grange_owner == "octo-cat"                       # esc drops the half-typed login
        assert all(a["owner"] == "octo-cat" for _, agents in grange_pushes for a in agents)

    _run(app, steps)


def test_a_bad_owner_or_port_in_the_file_falls_back(tmp_path, monkeypatch):
    p = tmp_path / "settings.json"
    monkeypatch.setenv("AGENT_FARM_SETTINGS", str(p))
    p.write_text(json.dumps({"grange_owner": "no spaces allowed", "grange_port": 70000}))
    s = Settings.load()
    assert s.grange_owner == "" and s.grange_port == grange.DEFAULT_PORT
    p.write_text(json.dumps({"grange_owner": "octo-cat", "grange_port": 7791, "grange_report": True}))
    s = Settings.load()
    assert (s.grange_owner, s.grange_port, s.grange_report) == ("octo-cat", 7791, True)
