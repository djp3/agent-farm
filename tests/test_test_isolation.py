"""Tests never touch the developer's own files (see conftest.py)."""
import settings
from settings import Settings


def test_a_test_never_sees_the_real_settings_file(tmp_path):
    assert Settings.path() == tmp_path / "settings.json"
    assert Settings.path() not in (settings.DEFAULT_PATH, settings.LEGACY_PATH)
    assert Settings.load() == Settings()                 # defaults, whatever the developer saved
