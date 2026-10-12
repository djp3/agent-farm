"""Usage accounting: once per message id across its content blocks, cost deltas count
only increases, and rows bucket by local day."""
import json
from datetime import datetime, timezone

from usage import UsageScanner


def _row(mid, out, ts, model="claude-x"):
    return json.dumps({"type": "assistant", "timestamp": ts, "sessionId": "s",
                       "message": {"id": mid, "model": model, "role": "assistant",
                                   "content": [{"type": "text", "text": "x"}],
                                   "usage": {"output_tokens": out, "input_tokens": 2}}})


def _cost(total, ts):
    return json.dumps({"type": "cost-state", "timestamp": ts, "totalCostUSD": total})


def _scan(tmp_path, lines):
    p = tmp_path / "t.jsonl"
    p.write_text("\n".join(lines) + "\n")
    u = UsageScanner()
    u._scan_transcript(str(p))
    u._publish()
    return u


def test_dedupe_cost_and_day_bucketing(tmp_path):
    now = datetime.now(tz=timezone.utc)
    ts = now.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    u = _scan(tmp_path, [
        _row("m1", 10, ts), _row("m1", 10, ts), _row("m1", 10, ts),     # three blocks, one message
        _row("m2", 5, ts, model="claude-y"),
        _cost(1.0, ts), _cost(1.5, ts), _cost(1.2, ts),                   # +1.0, +0.5, decrease ignored
    ])
    today = u.report(1)[0]
    assert today.messages == 2
    assert today.out_tokens == 15
    assert today.in_tokens == 4
    assert dict(today.out_by_model) == {"x": 10, "y": 5}
    assert abs(today.cost_usd - 1.5) < 1e-9
    assert today.session_count == 1
    assert u.model_split(1) == [("x", 10), ("y", 5)]
