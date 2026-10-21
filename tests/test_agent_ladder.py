"""Subagent states by time: running inside the grace period, running mid tool-call for up
to the stale limit, stale after it, dormant when idle, archived when older than the head
process or when the head is gone."""
import json
import os
import time

import collector
from collector import Collector, Instance


def _agent_dir(tmp_path, last_kind, mtime, meta_mtime=None):
    sub = tmp_path / "sess" / "subagents"
    sub.mkdir(parents=True, exist_ok=True)
    meta = sub / "agent-1.meta.json"
    meta.write_text(json.dumps({"name": "worker", "agentType": "general-purpose"}))
    m = meta_mtime if meta_mtime is not None else mtime
    os.utime(meta, (m, m))
    log = sub / "agent-1.jsonl"
    if last_kind == "tool_use":
        row = {"type": "assistant", "message": {"id": "x", "role": "assistant",
               "content": [{"type": "tool_use", "id": "t", "name": "Bash", "input": {}}]}}
    else:
        row = {"type": "assistant", "message": {"id": "x", "role": "assistant",
               "content": [{"type": "text", "text": "hello"}]}}
    log.write_text(json.dumps(row) + "\n")
    os.utime(log, (mtime, mtime))
    head = tmp_path / "sess.jsonl"
    head.write_text("")
    return head


def _status(tmp_path, last_kind, age_s, alive=True, proc_start_offset=-3600):
    now = time.time()
    head = _agent_dir(tmp_path, last_kind, now - age_s)
    inst = Instance(pid=1, session_id="sess", alive=alive, started_at=now + proc_start_offset)
    inst.transcript = collector.tail_for(head)
    [a] = Collector()._agents_for(inst, head)
    return a.status


def test_recent_activity_is_running(tmp_path):
    assert _status(tmp_path, "text", age_s=10) == "running"


def test_quiet_after_a_message_is_dormant(tmp_path):
    assert _status(tmp_path, "text", age_s=collector.SUBAGENT_RUNNING_GRACE_S + 5) == "dormant"


def test_mid_tool_call_stays_running_until_the_stale_limit(tmp_path):
    assert _status(tmp_path, "tool_use", age_s=collector.SUBAGENT_STALE_S - 30) == "running"


def test_mid_tool_call_past_the_limit_is_stale(tmp_path):
    assert _status(tmp_path, "tool_use", age_s=collector.SUBAGENT_STALE_S + 30) == "stale"


def test_older_than_the_head_process_is_archived(tmp_path):
    assert _status(tmp_path, "text", age_s=10, proc_start_offset=-5) == "archived"


def test_dead_head_archives_everything(tmp_path):
    assert _status(tmp_path, "text", age_s=10, alive=False) == "archived"


def test_stale_for_a_day_moves_to_the_archive(tmp_path):
    now = time.time()
    head = _agent_dir(tmp_path, "tool_use", now - collector.STALE_ARCHIVE_S - 60)
    inst = Instance(pid=1, session_id="sess", alive=True, started_at=now - 3 * 86400)   # this run
    inst.transcript = collector.tail_for(head)
    [a] = Collector()._agents_for(inst, head)
    assert a.status == "archived" and a.archived and a.stopped
    assert "stopped mid-task over a day ago" in __import__("main").detail_text(a, now).plain
