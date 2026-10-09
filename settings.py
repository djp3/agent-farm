"""
User settings for the monitor, persisted as JSON next to the code, or under
~/Library/Application Support/agent-farm when running as the bundled Mac app
(override the path with CLAUDE_MONITOR_SETTINGS). Command-line flags win for one run
but are not written back. Add a setting by adding a field with a default; unknown or
malformed keys in the file are ignored so an old file never breaks startup.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from apppaths import data_dir

log = logging.getLogger("monitor.settings")

DEFAULT_PATH = data_dir() / "monitor_settings.json"


@dataclass
class Settings:
    show_status_text: bool = False      # status word + age in rows (colorblind aid); color otherwise
    ordering: str = "attention"         # default instance ordering at launch
    show_usage: bool = False            # usage panel open at launch
    align_columns: bool = True          # pad names to a fixed column so activity text lines up
    name_column_width: int = 26         # width of that name column (edit here; not in the panel)

    @classmethod
    def path(cls) -> Path:
        return Path(os.environ.get("CLAUDE_MONITOR_SETTINGS") or DEFAULT_PATH)

    @classmethod
    def load(cls) -> "Settings":
        s = cls()
        p = cls.path()
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
