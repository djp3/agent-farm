"""Agents at work: when each session and subagent was running, rebuilt from transcript
rows, binned per clock hour as agents (running time over the hour), and drawn as stalks."""
import json
from datetime import datetime, timedelta, timezone

import main
from usage import ACTIVITY_GAP_S, UsageScanner

TOP = datetime.now().replace(minute=0, second=0, microsecond=0) - timedelta(hours=3)


def at(minutes: float) -> str:
    """A transcript timestamp `minutes` after TOP, in UTC as Claude Code writes it."""
    return (TOP + timedelta(minutes=minutes)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def row(kind: str, minutes: float, stop=None, text="x", meta=False) -> str:
    """Rows as Claude Code writes them: compact JSON, the timestamp after the message."""
    if kind == "assistant":
        msg = {"id": f"m{minutes}", "role": "assistant", "stop_reason": stop,
               "content": [{"type": "tool_use" if stop == "tool_use" else "text", "text": text}],
               "usage": {"output_tokens": 1, "input_tokens": 1}}
    else:
        msg = {"role": "user", "content": text}
    r = {"parentUuid": None, "type": kind, "message": msg, "uuid": "u", "timestamp": at(minutes)}
    if meta:
        r["isMeta"] = True
    return json.dumps(r, separators=(",", ":"))


def scan(tmp_path, *files: list[str]) -> UsageScanner:
    u = UsageScanner()
    for i, lines in enumerate(files):
        p = tmp_path / f"t{i}.jsonl"
        p.write_text("\n".join(lines) + "\n")
        u._scan_transcript(str(p))
    u._publish()
    return u


def hour(u: UsageScanner, k: int, now_minutes: float = 150) -> float:
    """Agents in the k-th hour after TOP, with the clock at TOP + now_minutes."""
    bins = u.activity(now=(TOP + timedelta(minutes=now_minutes)).timestamp())
    starts = [a for a, _ in bins]
    return bins[starts.index((TOP + timedelta(hours=k)).timestamp())][1]


def test_a_turn_runs_from_the_prompt_to_its_end(tmp_path):
    u = scan(tmp_path, [
        row("user", 0), row("assistant", 1, "tool_use"), row("user", 9), row("assistant", 18, "tool_use"),
        row("user", 20), row("assistant", 30, "end_turn"),    # 30 minutes of work
        row("user", 40, meta=True),                           # a local command: not the model
    ])
    assert abs(hour(u, 0) - 0.5) < 1e-9


def test_three_agents_half_an_hour_each_make_one_and_a_half(tmp_path):
    sub = [row("user", 0), row("assistant", 8), row("user", 10), row("assistant", 18), row("user", 20),
           row("assistant", 30, "end_turn")]
    u = scan(tmp_path, sub, sub, sub)                         # subagents leave stop_reason empty midway
    assert abs(hour(u, 0) - 1.5) < 1e-9


def test_a_silence_counts_ten_minutes_at_most(tmp_path):
    u = scan(tmp_path, [row("user", 0), row("assistant", 1, "tool_use"),   # then a prompt left open
                        row("user", 100), row("assistant", 101, "end_turn")])
    assert abs(hour(u, 0) - (1 + ACTIVITY_GAP_S / 60) / 60) < 1e-9
    assert abs(hour(u, 1) - 1 / 60) < 1e-9                   # 1:40 to 1:41


def test_an_interrupt_ends_the_turn(tmp_path):
    u = scan(tmp_path, [row("user", 0), row("assistant", 1, "tool_use"),
                        row("user", 2, text="[Request interrupted by user for tool use]")])
    assert abs(hour(u, 0) - 2 / 60) < 1e-9


def test_work_still_going_counts_up_to_now_and_the_current_hour_is_scaled(tmp_path):
    u = scan(tmp_path, [row("user", 120)] + [row("assistant", m, "tool_use") for m in range(125, 151, 5)])
    assert abs(hour(u, 2, now_minutes=150) - 1.0) < 1e-9     # all 30 minutes of the hour so far
    assert abs(hour(u, 2, now_minutes=170) - (40 / 50)) < 1e-9   # 2:00-2:40 of 50 minutes


def test_spans_before_the_last_day_are_not_kept(tmp_path):
    u = UsageScanner()
    u._horizon = (TOP + timedelta(minutes=60)).timestamp()
    p = tmp_path / "t.jsonl"
    p.write_text("\n".join([row("user", 0), row("assistant", 30, "end_turn"),
                            row("user", 90), row("assistant", 100, "end_turn")]) + "\n")
    u._scan_transcript(str(p))
    assert [(round(a), round(b)) for a, b in u._files[str(p)].spans] == [
        (round((TOP + timedelta(minutes=90)).timestamp()), round((TOP + timedelta(minutes=100)).timestamp()))]


def bins_of(vals):
    return [((TOP + timedelta(hours=k)).timestamp(), v) for k, v in enumerate(vals)]


def test_the_chart_draws_a_stalk_per_hour():
    vals = [0.0] * 21 + [1.5, 0.1, 3.0]
    now = (TOP + timedelta(hours=23, minutes=30)).timestamp()      # half way through the last hour
    lines = main.activity_text(bins_of(vals), 47, now=now).plain.splitlines()
    rows = lines[1:1 + main.ACTIVITY_ROWS]                    # top row first
    col = lambda k: [r[2 + k] for r in rows]                  # past "3┤"
    assert col(21) == [" ", " ", " ", main.GRAIN, main.STALK, main.STALK]   # 1.5 = three rows of 0.5
    assert col(22) == [" "] * 5 + [main.SPROUT]               # a little work still shows
    assert col(23) == [main.GRAIN] + [main.STALK] * 5          # 3.0 reaches the top
    assert [r[:2] for r in rows] == ["3┤", " │", "2┤", " │", "1┤", " │"]
    assert "now  3.0" in rows[0] and "peak 3.0" in rows[1]
    assert "avg  0.1" in rows[2]                              # 3.1 agent-hours over 23.5 hours
    assert "day  3.1 agent-h" in rows[3]
    assert all(len(x) <= 47 for x in lines)


def test_the_average_is_over_the_whole_chart():
    now = (TOP + timedelta(hours=23, minutes=15)).timestamp()
    rows = main.activity_text(bins_of([2.0] * 24), 47, now=now).plain.splitlines()
    assert "avg  2.0" in rows[3] and "day  46.5 agent-h" in rows[4]


def test_the_chart_scale_and_an_empty_day():
    assert main._activity_unit(2.2, 6) == 0.5 and main._activity_unit(0.3, 6) == 0.25
    assert main._activity_unit(7, 6) == 2 and main._activity_unit(100, 6) == 20
    assert "no agent has run" in main.activity_text(bins_of([0.0] * 24), 47).plain


def launch(minutes: float, tid: str, background: bool = True) -> str:
    """An assistant row issuing a Bash command, in the background or not."""
    inp = {"command": "npm run deploy", "description": "Deploy"}
    if background:
        inp["run_in_background"] = True
    msg = {"id": f"m{minutes}", "role": "assistant", "stop_reason": "tool_use",
           "content": [{"type": "tool_use", "id": tid, "name": "Bash", "input": inp}],
           "usage": {"output_tokens": 1, "input_tokens": 1}}
    return json.dumps({"parentUuid": None, "type": "assistant", "message": msg, "uuid": "u",
                       "timestamp": at(minutes), "sessionId": "S"}, separators=(",", ":"))


def notice(minutes: float, tid: str, status: str = "completed", queued: bool = True) -> str:
    """Claude Code's notice that a background task changed state: queued at once, and
    delivered to the agent as a user row when it next takes input."""
    body = (f"<task-notification><task-id>b1</task-id><tool-use-id>{tid}</tool-use-id>"
            f"<output-file>/tmp/b1.output</output-file><status>{status}</status>"
            f"<summary>done</summary></task-notification>")
    if not queued:
        return row("user", minutes, text=body)
    return json.dumps({"type": "queue-operation", "operation": "enqueue", "timestamp": at(minutes),
                       "sessionId": "S", "content": body}, separators=(",", ":"))


def test_an_agent_waiting_on_a_background_command_is_at_work(tmp_path):
    u = scan(tmp_path, [
        row("user", 0), launch(1, "t1"), row("user", 1.1, text="running in background"),
        row("assistant", 2, "end_turn"),                      # the turn ends with the command running
        notice(40, "t1"), notice(40, "t1", queued=False),     # it finishes, and wakes the agent
        row("assistant", 45, "end_turn"),
    ])
    assert abs(hour(u, 0) - 45 / 60) < 1e-9                   # 0:00-0:45, counted once, not 0:00-0:02 + 0:40-0:45


def test_an_unfinished_command_runs_to_now_only_while_its_shell_does(tmp_path):
    u = scan(tmp_path, [row("user", 120), launch(121, "t1"), row("assistant", 122, "end_turn")])
    now = (TOP + timedelta(minutes=150)).timestamp()
    started = (TOP + timedelta(minutes=121, seconds=2)).timestamp()
    capped = (1 + ACTIVITY_GAP_S / 60) / 30                   # 2:00-2:11: a silence, capped
    assert abs(u.activity(now=now, live_shells={"S": [started]})[-1][1] - 1.0) < 1e-9   # all of 2:00-2:30
    assert abs(u.activity(now=now)[-1][1] - capped) < 1e-9
    # another shell in the same session is some other command's, not this launch's
    assert abs(u.activity(now=now, live_shells={"S": [started + 3600]})[-1][1] - capped) < 1e-9


def test_progress_notices_from_a_monitor_are_not_an_end(tmp_path):
    u = scan(tmp_path, [row("user", 0), launch(1, "t1"), row("assistant", 2, "end_turn"),
                        notice(20, "t1", status="running"), notice(30, "t1", status="killed")])
    assert abs(hour(u, 0) - 30 / 60) < 1e-9


def test_a_command_moved_to_the_background_counts_once_it_reports(tmp_path):
    u = scan(tmp_path, [row("user", 0), launch(1, "t1", background=False),
                        row("user", 3, text="moved to the background"), row("assistant", 4, "end_turn"),
                        notice(30, "t1")])
    assert abs(hour(u, 0) - 30 / 60) < 1e-9
