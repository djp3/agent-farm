"""The usage scanner reads transcripts in bounded slices, skips files that cannot reach
the 14-day window, and complains about an unreadable file only now and then."""
import json
import logging
import os
import time
from datetime import datetime, timezone

import usage
from usage import UsageScanner


def _row(i, out=10):
    ts = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    return json.dumps({"type": "assistant", "timestamp": ts, "sessionId": "s1",
                       "message": {"id": f"msg-{i}", "model": "claude-x", "role": "assistant",
                                   "content": [{"type": "text", "text": "x" * 60}],
                                   "usage": {"output_tokens": out, "input_tokens": 1}}})


def _today_total(u, attr):
    u._publish()
    return getattr(u.report(1)[0], attr)


def test_old_file_is_skipped_until_it_is_written_again(tmp_path):
    p = tmp_path / "old.jsonl"
    p.write_text(_row(1) + "\n")
    stale = time.time() - 20 * 86400
    os.utime(p, (stale, stale))
    u = UsageScanner()
    assert u._scan_transcript(str(p)) is False
    assert _today_total(u, "messages") == 0
    with p.open("a") as f:                           # a resumed session appends one row
        f.write(_row(2) + "\n")
    assert u._scan_transcript(str(p)) is True
    assert _today_total(u, "messages") == 1          # only the new row, not the old one


def test_lines_straddling_slice_boundaries_are_counted_once(tmp_path, monkeypatch):
    monkeypatch.setattr(usage, "CHUNK_BYTES", 64)     # far smaller than one row
    p = tmp_path / "big.jsonl"
    p.write_text("".join(_row(i, out=7) + "\n" for i in range(25)))
    u = UsageScanner()
    assert u._scan_transcript(str(p)) is True
    assert _today_total(u, "messages") == 25
    assert _today_total(u, "out_tokens") == 25 * 7
    assert u._scan_transcript(str(p)) is False       # nothing new: offset landed on the end


def test_unreadable_path_warns_once_per_window(tmp_path, caplog):
    u = UsageScanner()
    with caplog.at_level(logging.WARNING, logger="agent_farm.usage"):
        u._scan_paths([str(tmp_path)])               # a directory: open() raises
        u._scan_paths([str(tmp_path)])
    assert sum("usage scan failed" in r.getMessage() for r in caplog.records) == 1
