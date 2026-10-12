"""Workers that finish after q must not log errors, and logging must exist before the
settings file is read so a corrupt file is reported rather than silently reset."""
import logging
import subprocess
import sys

import pytest

import main


def test_ui_call_after_shutdown_is_quiet(caplog):
    app = main.ClaudeMonitor()                       # never run: call_from_thread raises
    with caplog.at_level(logging.DEBUG, logger="monitor.app"):
        app._on_ui(lambda: None)
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


def test_other_runtime_errors_still_surface():
    app = main.ClaudeMonitor()

    def boom(*a, **k):
        raise RuntimeError("boom")
    app.call_from_thread = boom
    with pytest.raises(RuntimeError, match="boom"):
        app._on_ui(lambda: None)


def test_logging_is_ready_before_settings_load(tmp_path):
    # A fresh interpreter, so the file handler cannot already exist from an earlier test.
    bad = tmp_path / "monitor_settings.json"
    bad.write_text("{not json")
    code = f"""
import logging, os
os.environ["CLAUDE_MONITOR_SETTINGS"] = {str(bad)!r}
from logging.handlers import RotatingFileHandler
import main
seen = []
real = main.Settings.load
def probe():
    seen.append(any(isinstance(h, RotatingFileHandler) for h in logging.getLogger("monitor").handlers))
    return real()
main.Settings.load = staticmethod(probe)
app = main.ClaudeMonitor()
print("handler-before-load", seen[0], "defaults", app.settings.ordering)
"""
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         cwd=str(main.__file__.rsplit("/", 1)[0]))
    assert "handler-before-load True defaults attention" in out.stdout, out.stdout + out.stderr
