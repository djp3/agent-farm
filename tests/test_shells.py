"""Shells: Claude Code runs each command in a shell of its own under the session's process.
A session or subagent with nothing else going on while one runs is waiting on it, which
the tree shows as the teal 'shell' state, with the agent's own description of the command."""
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
    assert inst.shells[0].label == "Deploy the stack to dev" and inst.shells[0].owner == ""
    assert inst.activity.startswith("shell 1m") and inst.activity.endswith(": Deploy the stack to dev")


def test_a_subagents_shell_is_its_own_and_leaves_the_head_idle():
    inst, worker, now = _team()
    Collector._attribute_shells(inst, [Shell(pid=21, started=now - 59, command="pytest -q")])
    assert worker.status == "shell" and worker.shells[0].label == "Run the tests"
    assert inst.status == "idle" and inst.own_shells == [] and len(inst.shells) == 1
    assert worker.activity.endswith(": Run the tests")


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
    Collector._attribute_shells(inst, [Shell(pid=22, started=now - 5, command="./watch.sh\n--verbose")])
    assert inst.status == "shell" and inst.activity.endswith(": ./watch.sh")


def test_the_same_command_goes_to_whoever_issued_it_last_before_the_shell_started():
    inst, worker, now = _team()
    inst.transcript.commands["pytest -q"] = ("head's run", now - 600)
    Collector._attribute_shells(inst, [Shell(pid=21, started=now - 59, command="pytest -q")])
    assert worker.shells and inst.own_shells == []


def test_the_tree_shows_the_shell_state_in_teal():
    idle = Instance(pid=1, alive=True, status="idle")
    shell = Instance(pid=2, alive=True, status="shell")
    busy = Instance(pid=3, alive=True, status="busy")
    line = main.fleet_line([idle, shell, busy], False, 0, True)
    assert line.plain.startswith("■■■")
    assert [str(s.style) for s in line.spans[:3]] == [main.STATUS_STYLE[k] for k in ("busy", "shell", "idle")]
    assert main.STATUS_STYLE["shell"] == "#00afaf"
    assert "1 running a shell" in main.fleet_tooltip([idle, shell, busy], get_ordering("attention"), 0, True)
    glyphs = main.agent_glyphs([Agent(agent_id="a", name="a", status="shell"),
                                Agent(agent_id="b", name="b", status="running")])
    assert glyphs == [("●", main.STATUS_STYLE["running"]), ("◉", main.STATUS_STYLE["shell"])]


def test_the_detail_pane_lists_the_shells_with_their_commands():
    inst, worker, now = _team()
    Collector._attribute_shells(inst, [Shell(pid=20, started=now - 119, command="npm run deploy:dev"),
                                       Shell(pid=21, started=now - 59, command="pytest -q")])
    inst.status_since = now - 100
    text = main.detail_text(inst, now).plain
    assert "shell for 1m40s" in text and "shells  (commands still running)" in text
    assert "Deploy the stack to dev\n" in text and "$ npm run deploy:dev" in text
    assert "Run the tests  (worker)" in text


def test_the_attention_ordering_puts_a_shell_between_busy_and_idle():
    rows = [Instance(pid=1, alive=True, status="idle", status_since=100),
            Instance(pid=2, alive=True, status="shell", status_since=50),
            Instance(pid=3, alive=True, status="busy", status_since=10)]
    assert [i.pid for i in get_ordering("attention").sort(rows)] == [3, 2, 1]

