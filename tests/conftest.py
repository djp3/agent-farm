"""Shared test setup."""
import pytest

import collector


@pytest.fixture(autouse=True)
def no_real_iterm2(monkeypatch):
    """An app started in a test never opens the iTerm2 connection. Collector.start would ask
    the iTerm2 on this Mac for a pass and keep a thread connected to it for the rest of the
    run, and a reconnect from that thread lands in whichever test has faked the library."""
    monkeypatch.setattr(collector.Collector, "start", lambda self: None)


@pytest.fixture(autouse=True)
def no_real_settings(monkeypatch, tmp_path):
    """Every test gets its own empty settings file. Without this an app started in a test
    reads the checkout's settings.json: the result depends on what the developer saved, a
    test that changes a setting writes it back there, and with plan limits on the app would
    read the Claude Code sign-in from the Keychain and call the usage endpoint. A test that
    names its own file with monkeypatch.setenv still gets that one."""
    monkeypatch.setenv("AGENT_FARM_SETTINGS", str(tmp_path / "settings.json"))
    monkeypatch.delenv("CLAUDE_MONITOR_SETTINGS", raising=False)
