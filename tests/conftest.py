"""Shared test setup."""
import pytest

import collector
import grange


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


@pytest.fixture(autouse=True)
def grange_pushes(monkeypatch, tmp_path):
    """No test reaches the agent-grange on this Mac, whose port is the default one: an app with
    reporting on would put made-up sessions on the team's boards. The pushes are kept here,
    as (port, agents), for a test that asks for this fixture by name, and each is accepted.
    gh's sign-in is read from an empty folder, so the owner is only what a test sets. The
    tests of the real push hold their own reference to grange.put_farm, taken at import."""
    pushes = []

    def accept(port, agents):
        pushes.append((port, agents))
        return grange.Reply(204)

    monkeypatch.setattr(grange, "put_farm", accept)
    monkeypatch.setenv("GH_CONFIG_DIR", str(tmp_path / "gh"))
    return pushes


@pytest.fixture(autouse=True)
def grange_fresh_matching(monkeypatch):
    """Each test starts with no remembered pattern answers, so one test's timing (a slow
    pattern running out of time, say) is never another's."""
    monkeypatch.setattr(grange, "_decided", {})
