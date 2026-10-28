"""
Sessions Claude Code is about to delete, and the one write agent-farm makes: keep.

Claude Code deletes a session's transcript once it has gone `cleanupPeriodDays` (30 unless
~/.claude/settings.json says otherwise; 0 means never) without a change. It judges by the
file's modification time, so any new row restarts the clock. The sweep runs in the
background of a Claude Code process a few minutes after it starts, and takes the session's
folder (subagents, tool results) along with the transcript. Some parts have clocks of their
own even while the transcript lives: the tool-result files and the other files in that
folder, the files beside the transcript, and the session's checkpoints (file-history),
environment (session-env) and task list (tasks). Transcripts started from the Claude
desktop app are exempt.

Keep restarts every one of those clocks by setting the modification time to now. Nothing
is ever written into a file.
"""
from __future__ import annotations

import glob
import json
import logging
import os
import stat
import threading
import time
from dataclasses import dataclass, replace
from pathlib import Path

from collector import clean_text

CLAUDE_DIR = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude"))
DAY = 86400
DEFAULT_PERIOD_DAYS = 30            # Claude Code's own default for cleanupPeriodDays
WINDOW_DAYS = 14                    # how far ahead the pane looks
SOON_S = 2 * DAY                    # this close to deletion a session needs you
KEPT_SHOW_S = 60.0                  # a kept session stays in the list, green, this long
HEAD_BYTES = 64 * 1024              # entrypoint and cwd are in the first rows
TAIL_BYTES = 256 * 1024             # Claude Code re-appends the title rows; the last is near the end
# Started from the Claude desktop app: Claude Code never deletes these by age, unless the
# app has released them with a marker file beside the transcript.
EXEMPT_ENTRYPOINTS = frozenset({"claude-desktop", "claude-desktop-3p", "local-agent"})
RELEASED_SUFFIX = ".desktop-released.json"

log = logging.getLogger("agent_farm.expiring")


@dataclass(frozen=True)
class Expiring:
    session_id: str
    path: Path                      # the transcript
    last_used: float                # its modification time
    deletes_at: float               # when it turns cleanupPeriodDays old
    size: int = 0                   # transcript and session folder, in bytes
    title: str = ""
    cwd: str = ""
    kept_at: float = 0.0            # when keep ran on it, or 0

    def soon(self, now: float) -> bool:
        return self.deletes_at - now < SOON_S


def read_period(claude_dir: Path) -> float | None:
    """cleanupPeriodDays from Claude Code's user settings, in days; None when it is 0
    (Claude Code then never deletes). That file also holds secrets in its env block, so
    this takes the one key and nothing from the file reaches the log."""
    try:
        data = json.loads((claude_dir / "settings.json").read_text())
    except FileNotFoundError:
        return DEFAULT_PERIOD_DAYS
    except (OSError, ValueError) as e:
        log.warning("could not read Claude Code's settings.json (%s); assuming %d days",
                    type(e).__name__, DEFAULT_PERIOD_DAYS)
        return DEFAULT_PERIOD_DAYS
    v = data.get("cleanupPeriodDays") if isinstance(data, dict) else None
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0:
        return DEFAULT_PERIOD_DAYS
    return None if v == 0 else v


def _rows(chunk: bytes, needles: tuple[bytes, ...]):
    """The JSON rows in a slice of a transcript that mention one of the needles."""
    for raw in chunk.split(b"\n"):
        if any(n in raw for n in needles):
            try:
                row = json.loads(raw)
            except ValueError:          # a row cut by the slice
                continue
            if isinstance(row, dict):
                yield row


def _read_slices(path: Path, size: int) -> tuple[bytes, bytes]:
    with open(path, "rb") as fh:
        head = fh.read(HEAD_BYTES)
        if size <= HEAD_BYTES + TAIL_BYTES:
            return head + fh.read(), b""
        fh.seek(size - TAIL_BYTES)
        return head, fh.read()


def _folder_size(folder: Path) -> int:
    total = 0
    for dirpath, _, files in os.walk(folder):
        for name in files:
            try:
                total += os.lstat(os.path.join(dirpath, name)).st_size
            except OSError:
                pass
    return total


class ExpiryScanner:
    def __init__(self, claude_dir: Path | None = None):
        self.claude_dir = Path(claude_dir or CLAUDE_DIR)
        self.period_days: float | None = DEFAULT_PERIOD_DAYS
        self.scanned_once = False
        # path -> ((mtime, size), facts or None when exempt); old transcripts rarely change
        self._facts: dict[str, tuple[tuple[float, int], tuple | None]] = {}
        self._published: list[Expiring] = []
        self._kept: dict[str, Expiring] = {}
        self._lock = threading.Lock()
        self._busy = threading.Lock()

    # ---- public --------------------------------------------------------------
    def scan(self, now: float | None = None) -> bool:
        """Find the transcripts that turn cleanupPeriodDays old within WINDOW_DAYS. Returns
        True when the list changed, False when it did not or a scan is already running."""
        if not self._busy.acquire(blocking=False):
            return False
        try:
            return self._scan_locked(time.time() if now is None else now)
        finally:
            self._busy.release()

    def entries(self, now: float | None = None) -> list[Expiring]:
        """What the pane lists, soonest first: the sessions in the window, and the ones kept
        in the last KEPT_SHOW_S at the place they had."""
        now = time.time() if now is None else now
        with self._lock:
            kept = [e for e in self._kept.values() if now - e.kept_at < KEPT_SHOW_S]
            ids = {e.session_id for e in kept}
            return sorted([e for e in self._published if e.session_id not in ids] + kept,
                          key=lambda e: e.deletes_at)

    def keep(self, e: Expiring, now: float | None = None) -> int:
        """Give a session another full period: set the modification time of everything
        Claude Code judges it by to now. Returns how many files and folders that was."""
        now = time.time() if now is None else now
        n = 0
        for p in self._keep_targets(e):
            try:
                mode = os.lstat(p).st_mode
                if stat.S_ISREG(mode) or stat.S_ISDIR(mode):     # never through a symlink
                    os.utime(p, (now, now), follow_symlinks=False)
                    n += 1
            except OSError as err:
                log.warning("keep: could not touch %s (%s)", p.name, err.strerror or type(err).__name__)
        with self._lock:
            self._kept[e.session_id] = replace(e, kept_at=now)
            for sid in [s for s, k in self._kept.items() if now - k.kept_at >= KEPT_SHOW_S]:
                del self._kept[sid]
        log.info("kept session %s for another %s days (%d files and folders)",
                 e.session_id[:8], f"{self.period_days:g}" if self.period_days else "-", n)
        return n

    # ---- internals -------------------------------------------------------------
    def _scan_locked(self, now: float) -> bool:
        period = read_period(self.claude_dir)
        found: list[Expiring] = []
        seen: set[str] = set()
        if period is not None:
            for name in glob.glob(str(self.claude_dir / "projects" / "*" / "*.jsonl")):
                path = Path(name)
                try:
                    st = path.stat()
                except OSError:
                    continue
                deletes_at = st.st_mtime + period * DAY
                if deletes_at - now >= WINDOW_DAYS * DAY:
                    continue
                seen.add(name)
                facts = self._facts_for(path, st)
                if facts is None:
                    continue
                title, cwd, size = facts
                found.append(Expiring(path.stem, path, st.st_mtime, deletes_at, size, title, cwd))
        found.sort(key=lambda e: e.deletes_at)
        for name in [k for k in self._facts if k not in seen]:
            del self._facts[name]
        with self._lock:
            before = (self.period_days, [(e.session_id, e.deletes_at, e.title) for e in self._published])
            self.period_days, self._published = period, found
            after = (self.period_days, [(e.session_id, e.deletes_at, e.title) for e in self._published])
        self.scanned_once = True
        return before != after

    def _facts_for(self, path: Path, st: os.stat_result) -> tuple | None:
        """(title, cwd, size) of a transcript, or None when Claude Code exempts it."""
        key = (st.st_mtime, st.st_size)
        cached = self._facts.get(str(path))
        if cached and cached[0] == key:
            return cached[1]
        try:
            head, tail = _read_slices(path, st.st_size)
        except OSError as e:
            log.warning("could not read %s (%s)", path.name, e.strerror or type(e).__name__)
            return None
        entry = cwd = ""
        for row in _rows(head, (b'"entrypoint"', b'"cwd"')):
            entry = entry or str(row.get("entrypoint") or "")
            cwd = cwd or str(row.get("cwd") or "")
            if entry and cwd:
                break
        facts = None
        released = path.with_name(path.stem + RELEASED_SUFFIX).exists()
        if entry not in EXEMPT_ENTRYPOINTS or released:
            facts = (self._title(path, tail or head), clean_text(cwd), st.st_size + _folder_size(path.with_suffix("")))
        self._facts[str(path)] = (key, facts)
        return facts

    @staticmethod
    def _title(path: Path, tail: bytes) -> str:
        """The name /resume shows: the one you gave it, else the one Claude Code gave it."""
        try:
            named = json.loads((path.with_suffix("") / "custom-title.json").read_text()).get("customTitle")
        except (OSError, ValueError, AttributeError):
            named = None
        custom = ai = summary = None
        for row in _rows(tail, (b'"custom-title"', b'"ai-title"', b'"type":"summary"')):
            kind = row.get("type")
            if kind == "custom-title":
                custom = row.get("customTitle") or custom
            elif kind == "ai-title":
                ai = row.get("aiTitle") or row.get("title") or ai
            elif kind == "summary":
                summary = row.get("summary") or summary
        title = named or custom or ai or summary or ""
        return " ".join(clean_text(title).split())

    def _keep_targets(self, e: Expiring) -> list[Path]:
        project, sid = e.path.parent, e.session_id
        targets = [e.path]
        targets += [p for p in project.glob(glob.escape(sid) + ".*") if p != e.path]
        folder = project / sid
        if folder.is_dir() and not folder.is_symlink():
            targets += [p for p in folder.iterdir() if p.is_file()]
            for sub in ("tool-results", "mcp-tasks"):
                d = folder / sub
                if d.is_dir() and not d.is_symlink():
                    for p in d.iterdir():
                        if p.is_dir() and not p.is_symlink():
                            targets += [q for q in p.iterdir() if q.is_file()]
                        elif p.is_file():
                            targets.append(p)
        for side in (self.claude_dir / "file-history" / sid, self.claude_dir / "session-env" / sid,
                     self.claude_dir / "tasks" / f"session-{sid[:8]}"):
            if side.is_dir() and not side.is_symlink():
                targets.append(side)
        return targets
