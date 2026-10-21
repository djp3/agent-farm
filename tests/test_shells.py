"""Shells: Claude Code runs each command in a shell of its own under the session's process.
Each one that runs a while gets a row of its own under the agent that started it, with a
half-filled circle and the agent's description of it. An agent with nothing else going on
while one runs is waiting on it, and is drawn as idle; a shell whose agent has stopped is
stale, and gray."""
import asyncio
import json
import time

import main
from collector import Agent, Collector, Instance, Shell, TranscriptState, _shell_command
from ordering import get_ordering

WRAPPER = ("source /Users/x/.claude/shell-snapshots/snapshot-zsh-1-ab.sh 2>/dev/null || true && "
           "{ \\builtin unalias -- 'unsetenv'; } >/dev/null 2>&1 || true && "
           "eval {cmd} < /dev/null && pwd -P >| /tmp/claude-1-cwd")


def wrapped(cmd: str) -> str:
    """The script Claude Code hands its shell, with the command quoted the way it quotes it."""
    return WRAPPER.replace("{cmd}", "'" + cmd.replace("'", "'\"'\"'") + "'")


class FakeProc:
    def __init__(self, pid, ppid, argv, started=1000.0, error=None):
        self.info = {"pid": pid, "ppid": ppid, "name": argv[0].rsplit("/", 1)[-1] if argv else "?"}
        self._argv, self._started, self._error = argv, started, error

    def cmdline(self):
        if self._error:
            raise self._error
        return self._argv

    def create_time(self):
        return self._started


def test_the_command_comes_out_of_the_wrapper_unquoted():
    assert _shell_command(wrapped("npm run deploy:dev")) == "npm run deploy:dev"
    assert _shell_command(wrapped("grep -c 'a b' x | sort")) == "grep -c 'a b' x | sort"
    assert _shell_command("something else entirely") == "something else entirely"


def test_only_command_shells_under_a_live_head_count():
    procs = [
        FakeProc(20, 10, ["/bin/zsh", "-c", wrapped("npm test")], started=500.0),
        FakeProc(21, 10, ["caffeinate", "-i", "-t", "300"]),                  # Claude Code's own helper
        FakeProc(22, 10, ["node", "/opt/mcp/server.js"]),                       # an MCP server
        FakeProc(23, 99, ["/bin/zsh", "-c", wrapped("make")]),                  # not under a session
        FakeProc(24, 10, ["/bin/zsh"], error=PermissionError("gone")),          # unreadable: skipped
        FakeProc(25, 11, ["/bin/bash", "-c", wrapped("sleep 60")]),
    ]
    found = Collector._shells_under(procs, {10, 11})
    assert [(s.pid, s.command, s.started) for s in found[10]] == [(20, "npm test", 500.0)]
    assert [s.command for s in found[11]] == ["sleep 60"]


def test_a_transcript_remembers_the_commands_it_issued():
    tr = TranscriptState(path=None)
    tr._ingest(json.dumps({"type": "assistant", "timestamp": "2026-10-21T21:28:26.000Z", "message": {
        "id": "m", "role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {
            "command": "npm run deploy:dev", "description": "Deploy the stack to dev",
            "run_in_background": True}}]}}))
    summary, ts = tr.commands["npm run deploy:dev"]
    assert summary == "Deploy the stack to dev" and ts > 0


def _transcript(commands: dict) -> TranscriptState:
    tr = TranscriptState(path=None)
    tr.commands = dict(commands)
    return tr


def _team(head_status="idle", agent_status="dormant"):
    now = time.time()
    inst = Instance(pid=10, session_id="S", name="payable", alive=True, status=head_status)
    inst.transcript = _transcript({"npm run deploy:dev": ("Deploy the stack to dev", now - 120)})
    worker = Agent(agent_id="w1", name="worker", status=agent_status,
                   transcript=_transcript({"pytest -q": ("Run the tests", now - 60)}))
    inst.agents = [worker]
    return inst, worker, now


def test_an_idle_session_running_its_own_shell_is_in_the_shell_state():
    inst, worker, now = _team()
    Collector._attribute_shells(inst, [Shell(pid=20, started=now - 119, command="npm run deploy:dev")])
    assert inst.status == "shell" and worker.status == "dormant"
    sh = inst.shells[0]
    assert sh.label == "Deploy the stack to dev" and sh.owner == "" and sh.owner_name == "payable"
    assert main.look_of(inst) == main.SESSION_LOOK["idle"]          # drawn as idle, its shell beneath


def test_a_subagents_shell_is_its_own_and_leaves_the_head_idle():
    inst, worker, now = _team()
    Collector._attribute_shells(inst, [Shell(pid=21, started=now - 59, command="pytest -q")])
    assert worker.status == "shell" and worker.shells[0].label == "Run the tests"
    assert worker.shells[0].owner_name == "worker" and not worker.shells[0].stale
    assert inst.status == "idle" and inst.own_shells == [] and len(inst.shells) == 1
    assert main.look_of(worker) == main.AGENT_LOOK["dormant"]


def test_a_shell_whose_subagent_stopped_mid_task_is_stale():
    inst, worker, now = _team(agent_status="stale")
    Collector._attribute_shells(inst, [Shell(pid=21, started=now - 55, command="pytest -q")])
    assert worker.status == "stale" and worker.shells[0].stale
    assert main.look_of(worker.shells[0]) == main.SHELL_STALE
    text = main.detail_text(worker.shells[0], now).plain
    assert text.startswith("pytest -q\nstatus    stale for 55s  (the agent that started it has stopped")
    assert "for       Run the tests" in text and "agent     worker" in text and "command\npytest -q" in text


def test_busy_and_waiting_win_over_a_shell():
    for status in ("busy", "waiting"):
        inst, _, now = _team(head_status=status)
        Collector._attribute_shells(inst, [Shell(pid=20, started=now - 119, command="npm run deploy:dev")])
        assert inst.status == status
    inst, worker, now = _team(agent_status="running")
    Collector._attribute_shells(inst, [Shell(pid=21, started=now - 59, command="pytest -q")])
    assert worker.status == "running"


def test_an_unknown_command_belongs_to_the_head_under_its_own_name():
    inst, _, now = _team()
    Collector._attribute_shells(inst, [Shell(pid=22, started=now - 65, command="./watch.sh\n--verbose")])
    assert inst.status == "shell"
    assert main.shell_label(inst.shells[0], now, name_width=12).plain == "./watch.sh    1m05s"


def test_the_same_command_goes_to_whoever_issued_it_last_before_the_shell_started():
    inst, worker, now = _team()
    inst.transcript.commands["pytest -q"] = ("head's run", now - 600)
    Collector._attribute_shells(inst, [Shell(pid=21, started=now - 59, command="pytest -q")])
    assert worker.shells and inst.own_shells == []


def test_the_colors_say_state_and_white_names_say_yours_to_talk_to():
    idle = Instance(pid=1, alive=True, status="idle")
    shell = Instance(pid=2, alive=True, status="shell")
    busy = Instance(pid=3, alive=True, status="busy")
    waiting = Instance(pid=4, alive=True, status="waiting")
    line = main.fleet_line([idle, shell, busy, waiting], False, 0, True)
    assert line.plain.startswith("■■■■")
    assert [str(sp.style) for sp in line.spans[:3]] == [main.YELLOW, main.GREEN, main.BLUE]   # 1 + 1 + 2
    assert line.spans[2].end - line.spans[2].start == 2           # a session waiting on a shell is idle
    tip = main.fleet_tooltip([idle, shell, busy, waiting], get_ordering("attention"), 0, True)
    assert "2 idle (1 waiting on a shell)" in tip
    # sessions you can talk to have white names; anything at work is green, at rest blue
    assert main.look_of(waiting) == main.Look(main.YELLOW, main.WHITE, main.YELLOW)
    assert main.look_of(idle).name == main.look_of(shell).name == main.WHITE
    assert main.look_of(busy) == main.Look(main.GREEN, main.GREEN)
    assert main.look_of(Agent(agent_id="a", name="a", status="dormant")) == main.Look(main.BLUE, main.BLUE)
    assert main.look_of(Shell(pid=1, started=0, command="x")) == main.Look(main.GREEN, main.GREEN)
    row = main.instance_label(waiting, 0.0, name_width=8)
    assert row.spans[0].style == f"bold {main.WHITE}"


def test_the_detail_pane_lists_the_shells_with_their_commands():
    inst, worker, now = _team()
    Collector._attribute_shells(inst, [Shell(pid=20, started=now - 119, command="npm run deploy:dev"),
                                       Shell(pid=21, started=now - 59, command="pytest -q")])
    inst.status_since = now - 100
    text = main.detail_text(inst, now).plain
    assert "idle for 1m40s  (waiting on a command it started)" in text
    assert "shells  (commands still running)" in text
    assert "Deploy the stack to dev\n" in text and "$ npm run deploy:dev" in text
    assert "Run the tests  (worker)" in text


def test_the_attention_ordering_puts_a_shell_between_busy_and_idle():
    rows = [Instance(pid=1, alive=True, status="idle", status_since=100),
            Instance(pid=2, alive=True, status="shell", status_since=50),
            Instance(pid=3, alive=True, status="busy", status_since=10)]
    assert [i.pid for i in get_ordering("attention").sort(rows)] == [3, 2, 1]



def test_shells_get_rows_under_the_agent_that_started_them():
    """In the app: the head's own shell, then its subagents (the worker with its shell), then
    the archive; a shell younger than SHELL_ROW_AFTER_S has no row yet, and a shell that ends
    loses its row."""
    inst, worker, now = _team()
    old = Agent(agent_id="o1", name="old", status="archived", archived=True)
    inst.agents.append(old)
    shells = [Shell(pid=20, started=now - 119, command="npm run deploy:dev"),
              Shell(pid=21, started=now - 59, command="pytest -q"),
              Shell(pid=23, started=now - 1, command="ls")]                 # too quick for a row
    Collector._attribute_shells(inst, shells)
    snaps = [[inst]]

    async def go():
        app = main.AgentFarm()
        app.collector.snapshot = lambda: snaps[-1]
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause(1.5)
            tree = app.query_one("#tree")
            head = app.nodes["i:10"]
            kids = [str(c.data) for c in head.children]
            assert kids == ["i:10/s:20", "i:10/a:w1", "i:10/archived"]
            wnode = app.nodes["i:10/a:w1"]
            assert [str(c.data) for c in wnode.children] == ["i:10/a:w1/s:21"] and wnode.is_expanded
            assert "i:10/s:23" not in app.nodes
            label = tree.render_label(app.nodes["i:10/s:20"], main.Style(), main.Style())
            assert label.plain.startswith(main.AgentTree.ICON_SHELL + "npm run deploy:dev")
            assert app.nodes["i:10/s:20"].label.spans[0].style == f"bold {main.GREEN}"
            # the shell ends: its row goes, and the worker has nothing left to fold
            gone = Instance(pid=10, session_id="S", name="payable", alive=True, status="idle")
            gone.transcript, gone.agents = inst.transcript, [Agent(agent_id="w1", name="worker", status="dormant",
                                                                  transcript=worker.transcript), old]
            snaps.append([gone])
            await pilot.pause(1.5)
            assert [str(c.data) for c in app.nodes["i:10"].children] == ["i:10/a:w1", "i:10/archived"]
            assert not app.nodes["i:10/a:w1"].allow_expand
            await pilot.press("q")

    asyncio.run(go())
