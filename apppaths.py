"""
Where agent-farm keeps its own files.

Run from a checkout (the supervisor / `python main.py` flow) everything stays next to
the code: logs/ and settings.json. Run as the bundled Mac app
(PyInstaller sets sys.frozen) the bundle is signed and read-only, so logs go to
~/Library/Logs/agent-farm and settings to ~/Library/Application Support/agent-farm.
"""
from __future__ import annotations

import os
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


def app_version() -> str:
    """The release number without a leading v, or "" if unknown. The Mac app passes its
    bundle version in AGENT_FARM_VERSION; a run from a checkout reads the VERSION file."""
    v = os.environ.get("AGENT_FARM_VERSION", "").strip()
    if not v:
        try:
            v = (CODE_DIR / "VERSION").read_text().strip()
        except OSError:
            v = ""
    return v.removeprefix("v")
