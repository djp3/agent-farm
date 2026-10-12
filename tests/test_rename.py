"""The pre-rename names keep working for one release: CLAUDE_MONITOR_SETTINGS is read when
AGENT_FARM_SETTINGS is unset, and a monitor_settings.json is renamed to settings.json."""
import json

import settings
from settings import Settings


def test_new_env_var_wins_and_old_one_still_works(monkeypatch, tmp_path):
    monkeypatch.delenv("AGENT_FARM_SETTINGS", raising=False)
    monkeypatch.setenv("CLAUDE_MONITOR_SETTINGS", str(tmp_path / "old.json"))
    assert Settings.path() == tmp_path / "old.json"
    monkeypatch.setenv("AGENT_FARM_SETTINGS", str(tmp_path / "new.json"))
    assert Settings.path() == tmp_path / "new.json"


def test_legacy_settings_file_is_renamed_once(monkeypatch, tmp_path):
    monkeypatch.delenv("AGENT_FARM_SETTINGS", raising=False)
    monkeypatch.delenv("CLAUDE_MONITOR_SETTINGS", raising=False)
    new, old = tmp_path / "settings.json", tmp_path / "monitor_settings.json"
    monkeypatch.setattr(settings, "DEFAULT_PATH", new)
    monkeypatch.setattr(settings, "LEGACY_PATH", old)
    old.write_text(json.dumps({"show_usage": True, "ordering": "name"}))
    s = Settings.load()
    assert s.show_usage is True and s.ordering == "name"
    assert new.exists() and not old.exists()
    s.save()                                         # writes to the new name from now on
    assert json.loads(new.read_text())["ordering"] == "name"
