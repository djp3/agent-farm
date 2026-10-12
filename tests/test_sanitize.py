"""Everything agent-farm shows comes from files another program wrote and is drawn in a
terminal, so control characters are removed where text enters the model."""
import json

from collector import clean_text, tail_for

ESC = "\x1b"
BEL = "\x07"


def test_clean_text_strips_terminal_controls_but_keeps_text():
    s = f"before {ESC}]52;c;SGVsbG8={BEL} after {ESC}[31mred\x9b0m\ttab\nline"
    out = clean_text(s)
    assert ESC not in out and BEL not in out and "\x9b" not in out
    assert "before" in out and "after" in out and "\t" in out and "\n" in out


def test_clean_text_handles_missing_values_and_bidi_overrides():
    assert clean_text(None) == ""
    assert clean_text(12) == "12"
    assert clean_text("a‮b⁦c") == "abc"


def test_transcript_rows_are_sanitized_at_ingest(tmp_path):
    rows = [
        {"type": "user", "timestamp": "2026-10-09T10:00:00Z",
         "message": {"role": "user", "content": f"do {ESC}]52;c;QUJD{BEL} this"}},
        {"type": "assistant", "timestamp": "2026-10-09T10:00:01Z",
         "message": {"id": "m1", "role": "assistant",
                     "content": [{"type": "text", "text": f"ok {ESC}[2J wiped"}]}},
        {"type": "assistant", "timestamp": "2026-10-09T10:00:02Z",
         "message": {"id": "m2", "role": "assistant",
                     "content": [{"type": "tool_use", "id": "t1", "name": f"Bash{ESC}c",
                                  "input": {"command": f"ls {ESC}]0;evil{BEL}", "description": "list"}}]}},
        {"type": "queue-operation", "timestamp": "2026-10-09T10:00:03Z",
         "operation": "enqueue", "content": f"later {ESC}[?1049h"},
    ]
    p = tmp_path / "session.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    t = tail_for(p)
    assert ESC not in t.last_prompt and "this" in t.last_prompt
    assert ESC not in t.last_text and "wiped" in t.last_text
    tool = t.current_tool
    assert tool is not None and ESC not in tool[0] and ESC not in tool[1]
    assert t.queued and all(ESC not in q["content"] for q in t.queued)
