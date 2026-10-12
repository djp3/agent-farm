"""A teammate is dormant only while its own transcript has not changed since its last reply
reached the head. Teammates stay resident and can be given more work, so "finished" must
be a moment, not a permanent mark."""
import json
import os
import time
from datetime import datetime, timezone

from collector import Collector, Instance, tail_for


def _iso(t):
    return datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _setup(tmp_path, agent_mtime, reply_at):
    head = tmp_path / "sess.jsonl"
    head.write_text(json.dumps({
        "type": "user", "timestamp": _iso(reply_at),
        "message": {"role": "user", "content": '<teammate-message teammate_id="worker">done</teammate-message>'},
    }) + "\n")
    sub = tmp_path / "sess" / "subagents"
    sub.mkdir(parents=True)
    meta = sub / "agent-1.meta.json"
    meta.write_text(json.dumps({"name": "worker", "agentType": "general-purpose"}))
    os.utime(meta, (reply_at - 500, reply_at - 500))
    log = sub / "agent-1.jsonl"
    log.write_text(json.dumps({"type": "assistant", "timestamp": _iso(agent_mtime),
                               "message": {"id": "m1", "role": "assistant",
                                           "content": [{"type": "text", "text": "working"}]}}) + "\n")
    os.utime(log, (agent_mtime, agent_mtime))
    inst = Instance(pid=4242, session_id="sess", name="head", alive=True, started_at=reply_at - 1000)
    inst.transcript = tail_for(head)
    return inst, head


def test_quiet_since_its_reply_is_dormant(tmp_path):
    now = time.time()
    inst, head = _setup(tmp_path, agent_mtime=now - 400, reply_at=now - 300)
    [agent] = Collector()._agents_for(inst, head)
    assert agent.status == "dormant"


def test_working_again_after_its_reply_is_running(tmp_path):
    now = time.time()
    inst, head = _setup(tmp_path, agent_mtime=now - 5, reply_at=now - 300)
    [agent] = Collector()._agents_for(inst, head)
    assert agent.status == "running"
