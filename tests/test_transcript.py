"""The transcript reader's bookkeeping: queued prompts, permission waits, teammate relays
kept out of the prompt line, and the running tool."""
import json

from collector import tail_for

TS = "2026-10-12T10:00:0{}Z"


def _user(text, n=0, meta=False):
    row = {"type": "user", "timestamp": TS.format(n), "message": {"role": "user", "content": text}}
    if meta:
        row["isMeta"] = True
    return row


def _tool_use(n, name="Bash", tid="t1"):
    return {"type": "assistant", "timestamp": TS.format(n),
            "message": {"id": f"m{n}", "role": "assistant",
                        "content": [{"type": "tool_use", "id": tid, "name": name, "input": {"command": "ls"}}]}}


def _tool_result(n, tid="t1"):
    return {"type": "user", "timestamp": TS.format(n),
            "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tid}]}}


def _queue(op, n, content=""):
    return {"type": "queue-operation", "timestamp": TS.format(n), "operation": op, "content": content}


def _attachment(event, n):
    return {"type": "attachment", "timestamp": TS.format(n), "attachment": {"hookEvent": event}}


def _write(tmp_path, rows):
    p = tmp_path / "s.jsonl"
    p.write_text("".join(json.dumps(r) + "\n" for r in rows))
    return tail_for(p)


def test_queue_operations(tmp_path):
    t = _write(tmp_path, [_queue("enqueue", 1, "first"), _queue("enqueue", 2, "second"),
                          _queue("enqueue", 3, "third"), _queue("dequeue", 4),
                          _queue("remove", 5, "third")])
    assert [q["content"] for q in t.queued] == ["second"]
    t = _write(tmp_path / "b", [_queue("enqueue", 1, "a"), _queue("enqueue", 2, "b"), _queue("popAll", 3)]) \
        if (tmp_path / "b").mkdir() is None else None
    assert t.queued == []


def test_permission_request_waits_until_the_tool_result(tmp_path):
    t = _write(tmp_path, [_tool_use(1), _attachment("PermissionRequest", 2)])
    assert t.permission_pending is True
    t2 = _write(tmp_path / "later", [_tool_use(1), _attachment("PermissionRequest", 2), _tool_result(3)]) \
        if (tmp_path / "later").mkdir() is None else None
    assert t2.permission_pending is False
    assert t2.current_tool is None                      # the result closed the tool


def test_running_tool_and_relay_filtering(tmp_path):
    t = _write(tmp_path, [_user("please build it", 1), _tool_use(2, name="Read", tid="r1"),
                          _user('<teammate-message teammate_id="bot">done</teammate-message>', 3),
                          _user("<system-reminder>x</system-reminder>", 4),
                          _user("hidden", 5, meta=True)])
    assert t.last_prompt == "please build it"          # relays, markup and meta rows never win
    tool = t.current_tool
    assert tool is not None and tool[0] == "Read"
    assert "bot" in t.finished_agents
