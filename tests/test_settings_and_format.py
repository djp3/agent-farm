"""Settings loading tolerates bad files; the small formatters and glyph rules behave."""
import json

import main
from collector import Agent, fmt_age, fmt_tokens
from settings import Settings
from usage import fmt_count


def test_settings_ignore_wrong_types_and_unknown_keys(tmp_path, monkeypatch):
    p = tmp_path / "settings.json"
    monkeypatch.setenv("AGENT_FARM_SETTINGS", str(p))
    p.write_text(json.dumps({"show_usage": "yes", "ordering": "name", "mystery": 1, "name_column_width": "26"}))
    s = Settings.load()
    assert s.show_usage is False            # wrong type: default kept
    assert s.ordering == "name"
    assert s.name_column_width == 26        # wrong type: default kept
    s.align_columns = False
    s.save()
    assert Settings.load().align_columns is False


def test_settings_missing_or_corrupt_file_gives_defaults(tmp_path, monkeypatch):
    p = tmp_path / "settings.json"
    monkeypatch.setenv("AGENT_FARM_SETTINGS", str(p))
    assert Settings.load() == Settings()
    p.write_text("{oops")
    assert Settings.load() == Settings()


def test_formatters():
    assert fmt_age(None) == "—"
    assert fmt_age(1000, now=1045) == "45s"
    assert fmt_age(1000, now=1000 + 3 * 60 + 5) == "3m05s"
    assert fmt_age(1000, now=1000 + 2 * 3600 + 60) == "2h01m"
    assert fmt_age(1000, now=1000 + 86400 + 3600) == "1d1h"
    assert (fmt_tokens(999), fmt_tokens(72_000), fmt_tokens(1_500_000)) == ("999", "72k", "1.5M")
    assert (fmt_count(12), fmt_count(3_175), fmt_count(3_723_879), fmt_count(2e9)) == ("12", "3k", "3.7M", "2.0B")


def test_bar_fills_proportionally_and_pads():
    assert main._bar(0.0, 10) == " " * 10
    assert main._bar(1.0, 10) == "█" * 10
    assert len(main._bar(0.37, 10)) == 10 and main._bar(0.37, 10).startswith("███")
    assert main._bar(2.0, 4) == "████"          # clamped


def test_agent_strip_glyphs_five_per_asterisk_and_cap():
    def agents(n, status):
        return [Agent(agent_id=str(k), name=f"a{k}", status=status) for k in range(n)]
    g = main.agent_glyphs(agents(7, "running"))
    assert [x for x, _ in g] == ["✱", "●", "●"]
    g = main.agent_glyphs(agents(2, "dormant") + agents(1, "running") + agents(1, "stale"))
    assert [x for x, _ in g] == ["●", "◍", "○", "○"]         # running → stale → dormant
    archived = agents(3, "archived")
    for a in archived:
        a.archived = True
    assert main.agent_glyphs(archived) == []
    big = main.agent_glyphs(agents(5 * main.STRIP_MAX + 10, "running"))
    assert len(big) == main.STRIP_MAX and big[-1][0] == "+"


def test_queued_summary_names_relays():
    assert main._queued_summary("fix the tests") == "fix the tests"
    assert main._queued_summary('<teammate-message teammate_id="bot">hi</teammate-message>') == "message from teammate bot"
    assert main._queued_summary('<task-notification id="1">x</task-notification>') == "background task finished (result pending delivery)"
    assert main._queued_summary('<cross-session-message from="other">x</cross-session-message>') == "message from another session (other)"
