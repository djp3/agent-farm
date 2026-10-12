"""
Token-usage aggregation across every Claude Code transcript on this machine.

Read-only. It tails ~/.claude/projects/**/*.jsonl (head sessions and subagents) and
buckets per local day:

  output tokens, input tokens (incl. cache reads/writes), API messages, sessions active,
  output tokens per model, and Claude Code's own cost estimate where a session wrote a
  `cost-state` row.

Claude Code writes one transcript row per content block of an API message, each
carrying the *same* usage object, so usage is counted once per message id.
No prices are hard-coded here: cost comes only from Claude Code's cost-state rows.
"""
from __future__ import annotations

import glob
import json
import logging
import os
import threading
import time
from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Optional

CLAUDE_DIR = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude"))
PROJECTS_DIR = CLAUDE_DIR / "projects"
WINDOW_DAYS = 14                    # what the panel shows; older files are never read
CHUNK_BYTES = 8 * 1024 * 1024       # transcripts are read in slices, never whole (one here is 89 MB)
WARN_EVERY_S = 900                  # per-file read failures are logged at most this often

log = logging.getLogger("agent_farm.usage")

_NEEDLES = (b'"usage"', b'"cost-state"')


def _local_day(ts: Optional[str]) -> Optional[str]:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone().strftime("%Y-%m-%d")
    except ValueError:
        return None


@dataclass
class DayUsage:
    date: str
    out_tokens: int = 0
    in_tokens: int = 0              # input + cache_read + cache_creation
    messages: int = 0
    cost_usd: float = 0.0
    sessions: set = field(default_factory=set)
    out_by_model: Counter = field(default_factory=Counter)

    @property
    def session_count(self) -> int:
        return len(self.sessions)


@dataclass
class _FileState:
    offset: int = 0
    partial: bytes = b""
    size: int = -1
    mtime: float = -1.0
    last_msg_id: str = ""
    last_ts: str = ""
    last_cost: float = 0.0           # last cost-state total seen, for deltas
    session_id: str = ""


class UsageScanner:
    def __init__(self):
        # The scanner thread's working tallies. The UI never reads these: at the end of
        # each scan a frozen copy is published, so a redraw on the main thread can never
        # iterate a Counter the scanner is adding a key to.
        self.days: dict[str, DayUsage] = {}
        self._published: dict[str, DayUsage] = {}
        self._publish_lock = threading.Lock()
        self._files: dict[str, _FileState] = {}
        self._warned: dict[str, float] = {}
        self.last_scan_duration = 0.0
        self.scanned_once = False
        self.file_count = 0
        self._busy = threading.Lock()           # a scan that overruns must not overlap the next

    # ---- public --------------------------------------------------------------
    def scan(self) -> bool:
        """Incrementally read new bytes from every transcript. Returns True if changed,
        False when nothing changed or the previous scan is still running."""
        if not self._busy.acquire(blocking=False):
            return False
        try:
            return self._scan_locked()
        finally:
            self._busy.release()

    def _scan_locked(self) -> bool:
        t0 = time.time()
        changed = False
        paths = glob.glob(str(PROJECTS_DIR / "*" / "*.jsonl")) + \
            glob.glob(str(PROJECTS_DIR / "*" / "*" / "subagents" / "*.jsonl"))
        self.file_count = len(paths)
        changed |= self._scan_paths(paths)
        self.last_scan_duration = time.time() - t0
        if not self.scanned_once:
            log.info("usage: first scan of %d transcripts took %.2fs", len(paths), self.last_scan_duration)
        self._publish()
        self.scanned_once = True
        return changed

    def _scan_paths(self, paths: list[str]) -> bool:
        changed = False
        for p in paths:
            try:
                changed |= self._scan_transcript(p)
            except Exception as e:           # unreadable or malformed file: skip it, say so rarely
                self._warn(p, e)
        return changed

    def _warn(self, path: str, exc: Exception) -> bool:
        now = time.time()
        if now - self._warned.get(path, 0.0) < WARN_EVERY_S:
            return False
        self._warned[path] = now
        log.warning("usage scan failed for %s: %r", path, exc)
        return True

    def _publish(self) -> None:
        """Replace the readable snapshot with independent copies of the working tallies."""
        frozen = {date: replace(d, sessions=set(d.sessions), out_by_model=Counter(d.out_by_model))
                  for date, d in self.days.items()}
        with self._publish_lock:
            self._published = frozen

    def report(self, n_days: int = 14) -> list[DayUsage]:
        """The last n_days of local days, oldest first, zero-filled. Reads the snapshot
        published by the last completed scan, never the scanner's working state."""
        with self._publish_lock:
            published = self._published
        today = datetime.now().date()
        out = []
        for i in range(n_days - 1, -1, -1):
            d = (today.fromordinal(today.toordinal() - i)).strftime("%Y-%m-%d")
            out.append(published.get(d) or DayUsage(date=d))
        return out

    def model_split(self, n_days: int = 14) -> list[tuple[str, int]]:
        c: Counter = Counter()
        for d in self.report(n_days):
            c.update(d.out_by_model)
        return c.most_common()

    # ---- internals -----------------------------------------------------------
    def _day(self, date: str) -> DayUsage:
        d = self.days.get(date)
        if d is None:
            d = self.days[date] = DayUsage(date=date)
        return d

    def _new_lines(self, path: str, st: _FileState):
        """Yield the complete lines appended since the last call, reading CHUNK_BYTES at a
        time so memory stays bounded by a slice plus one partial line. A file first seen
        with a modification time before the window is skipped: nothing in it can reach
        the panel, and if it is written again later only the new bytes are read."""
        try:
            s = os.stat(path)
        except FileNotFoundError:
            return
        if s.st_size == st.size and s.st_mtime == st.mtime:
            return
        if st.size < 0 and s.st_mtime < time.time() - (WINDOW_DAYS + 1) * 86400:
            st.offset, st.size, st.mtime = s.st_size, s.st_size, s.st_mtime
            return
        st.size, st.mtime = s.st_size, s.st_mtime
        if s.st_size < st.offset:                    # truncated: start over
            st.offset, st.partial = 0, b""
        with open(path, "rb") as f:
            f.seek(st.offset)
            while True:
                data = f.read(CHUNK_BYTES)
                if not data:
                    break
                lines = (st.partial + data).split(b"\n")
                st.partial = lines.pop()
                st.offset = f.tell() - len(st.partial)
                yield from lines

    def _scan_transcript(self, path: str) -> bool:
        st = self._files.get(path)
        if st is None:
            st = self._files[path] = _FileState()
        changed = False
        for raw in self._new_lines(path, st):
            if not any(n in raw for n in _NEEDLES):
                continue
            try:
                row = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(row, dict):
                continue
            t = row.get("type")
            ts = row.get("timestamp") or st.last_ts
            if row.get("timestamp"):
                st.last_ts = row["timestamp"]
            if row.get("sessionId"):
                st.session_id = row["sessionId"]
            if t == "assistant":
                msg = row.get("message") or {}
                usage = msg.get("usage") if isinstance(msg, dict) else None
                if not isinstance(usage, dict):
                    continue
                mid = msg.get("id") or ""
                if mid and mid == st.last_msg_id:            # another block of the same message
                    continue
                st.last_msg_id = mid
                day = _local_day(ts)
                if not day:
                    continue
                d = self._day(day)
                out = int(usage.get("output_tokens") or 0)
                inp = int(usage.get("input_tokens") or 0)
                cr = int(usage.get("cache_read_input_tokens") or 0)
                cw = int(usage.get("cache_creation_input_tokens") or 0)
                d.out_tokens += out
                d.in_tokens += inp + cr + cw
                d.messages += 1
                model = (msg.get("model") or "?").replace("claude-", "")
                d.out_by_model[model] += out
                if st.session_id:
                    d.sessions.add(st.session_id)
                changed = True
            elif t == "cost-state":
                try:
                    total = float(row.get("totalCostUSD") or 0.0)
                except (TypeError, ValueError):
                    continue
                delta = total - st.last_cost
                st.last_cost = total
                day = _local_day(ts)
                if day and delta > 0:
                    self._day(day).cost_usd += delta
                    changed = True
        return changed



def fmt_count(n: float) -> str:
    n = float(n)
    if n >= 1e9:
        return f"{n / 1e9:.1f}B"
    if n >= 1e6:
        return f"{n / 1e6:.1f}M"
    if n >= 1e3:
        return f"{n / 1e3:.0f}k"
    return f"{int(n)}"


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    u = UsageScanner()
    u.scan()
    print(f"{u.file_count} transcripts, scan {u.last_scan_duration:.2f}s")
    for d in u.report(14):
        print(f"{d.date}  out {fmt_count(d.out_tokens):>6}  in {fmt_count(d.in_tokens):>6}  "
              f"msgs {d.messages:>5}  sessions {d.session_count:>2}  "
              f"cost ${d.cost_usd:7.2f}")
    print("model split:", u.model_split(14)[:5])
