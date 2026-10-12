"""A transcript line that fails to parse is reported by file, error and size, never by
content: transcripts hold prompts and pasted text that must not end up in the log."""
import logging

import collector
from collector import tail_for


def test_bad_row_content_stays_out_of_the_log(tmp_path, caplog, monkeypatch):
    monkeypatch.setattr(collector, "_debounce", collector._Debounce(every_s=0))
    secret = "hunter2-SECRET-TOKEN"
    p = tmp_path / "session.jsonl"
    # Valid JSON whose shape makes the handler raise: this is the row the log line sees.
    p.write_text('{"type": "user", "message": {"content": "fine"}}\n'
                 '{"type": "user", "timestamp": "2026-10-09T10:00:00Z", "message": "' + secret + '"}\n')
    with caplog.at_level(logging.WARNING, logger="agent_farm.collector"):
        t = tail_for(p)
    assert t.bad_rows == 1
    messages = [r.getMessage() for r in caplog.records]
    assert any("bad transcript row in session.jsonl" in m for m in messages)
    assert all(secret not in m for m in messages)
    assert any("bytes" in m for m in messages)
