"""The usage panel is drawn on the main thread while the scanner thread updates its
tallies; the panel must only ever see a frozen copy published at the end of a scan."""
from collections import Counter
from datetime import datetime

from usage import UsageScanner


def _today():
    return datetime.now().date().strftime("%Y-%m-%d")


def test_report_is_empty_until_a_scan_publishes():
    u = UsageScanner()
    d = u._day(_today())
    d.out_tokens = 5
    assert u.report(1)[0].out_tokens == 0          # working tallies are not visible yet
    u._publish()
    assert u.report(1)[0].out_tokens == 5


def test_published_copy_is_independent_of_later_scanner_writes():
    u = UsageScanner()
    d = u._day(_today())
    d.out_tokens, d.messages = 5, 2
    d.out_by_model["claude-a"] = 5
    d.sessions.add("s1")
    u._publish()
    # The scanner keeps working on the next scan: a new model key, more tokens, a session.
    d.out_by_model["claude-b"] = 1
    d.out_tokens = 99
    d.sessions.add("s2")
    seen = u.report(1)[0]
    assert seen.out_tokens == 5 and seen.session_count == 1
    assert seen.out_by_model == Counter({"claude-a": 5})
    assert u.model_split(1) == [("claude-a", 5)]
    assert seen.out_by_model is not d.out_by_model and seen.sessions is not d.sessions
