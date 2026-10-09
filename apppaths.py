"""
Where the monitor keeps its own files.

Run from a checkout (the supervisor / `python main.py` flow) everything stays next to
the code, as before: logs/ and monitor_settings.json. Run as the bundled Mac app
(PyInstaller sets sys.frozen) the bundle is signed and read-only, so logs go to
~/Library/Logs/agent-farm and settings to ~/Library/Application Support/agent-farm.
"""
from __future__ import annotations

import sys
from pathlib import Path

APP_NAME = "agent-farm"
FROZEN = bool(getattr(sys, "frozen", False))
CODE_DIR = Path(__file__).resolve().parent


def log_dir() -> Path:
    if FROZEN:
        return Path.home() / "Library" / "Logs" / APP_NAME
    return CODE_DIR / "logs"


def data_dir() -> Path:
    if FROZEN:
        return Path.home() / "Library" / "Application Support" / APP_NAME
    return CODE_DIR
