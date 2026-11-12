"""Shared test setup."""
import pytest

import collector


@pytest.fixture(autouse=True)
def no_real_iterm2(monkeypatch):
    """An app started in a test never opens the iTerm2 connection. Collector.start would ask
    the iTerm2 on this Mac for a pass and keep a thread connected to it for the rest of the
    run, and a reconnect from that thread lands in whichever test has faked the library."""
    monkeypatch.setattr(collector.Collector, "start", lambda self: None)
