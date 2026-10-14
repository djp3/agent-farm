"""
User settings for agent-farm, persisted as settings.json next to the code, or under
~/Library/Application Support/agent-farm when running as the bundled Mac app
(override the path with AGENT_FARM_SETTINGS). Command-line flags win for one run
but are not written back. Add a setting by adding a field with a default; unknown or
malformed keys in the file are ignored so an old file never breaks startup.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, fields
from pathlib import Path

from apppaths import data_dir

log = logging.getLogger("agent_farm.settings")

DEFAULT_PATH = data_dir() / "settings.json"
LEGACY_PATH = data_dir() / "monitor_settings.json"     # the name before the project was agent-farm


@dataclass
class Settings:
    show_status_text: bool = False      # status word + age in rows (colorblind aid); color otherwise
    ordering: str = "attention"         # default instance ordering at launch
    show_usage: bool = False            # usage panel open at launch
    align_columns: bool = True          # pad names to a fixed column so activity text lines up
    show_limits: bool = False           # plan limits (/usage) in the usage panel; reads the Keychain sign-in
    name_column_width: int = 26         # width of that name column (edit here; not in the panel)

    @classmethod
    def path(cls) -> Path:
        override = os.environ.get("AGENT_FARM_SETTINGS") or os.environ.get("CLAUDE_MONITOR_SETTINGS")
        return Path(override or DEFAULT_PATH)

    @classmethod
    def _migrate_legacy(cls, p: Path) -> None:
        """Carry a monitor_settings.json over to settings.json once, so nothing is lost."""
        if p == DEFAULT_PATH and not p.exists() and LEGACY_PATH.exists():
            try:
                os.replace(LEGACY_PATH, p)
                log.info("moved %s to %s", LEGACY_PATH.name, p.name)
            except OSError as e:
                log.warning("could not rename %s: %r", LEGACY_PATH, e)

    @classmethod
    def load(cls) -> "Settings":
        s = cls()
        p = cls.path()
        cls._migrate_legacy(p)
        try:
            data = json.loads(p.read_text())
        except FileNotFoundError:
            return s
        except Exception as e:
            log.warning("could not read %s: %r (using defaults)", p, e)
            return s
        if not isinstance(data, dict):
            return s
        for f in fields(cls):
            if f.name in data and isinstance(data[f.name], type(getattr(s, f.name))):
                setattr(s, f.name, data[f.name])
        return s

    def save(self) -> None:
        p = self.path()
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(".tmp")
            tmp.write_text(json.dumps(asdict(self), indent=2) + "\n")
            os.replace(tmp, p)
        except Exception as e:
            log.warning("could not write %s: %r", p, e)
