"""
Token-usage aggregation across every Claude Code transcript on this machine.

Read-only. It tails ~/.claude/projects/**/*.jsonl (head sessions and subagents) and
~/.claude/history.jsonl, and buckets per local day:

  output tokens, input tokens (incl. cache reads/writes), API messages, prompts typed,
  sessions active, output tokens per model, and Claude Code's own cost estimate where a
  session wrote a `cost-state` row.

Claude Code writes one transcript row per content block of an API message, each
carrying the *same* usage object, so usage is counted once per message id.
No prices are hard-coded here: cost comes only from Claude Code's cost-state rows.
"""
from __future__ import annotations

import glob
import json
import logging
import os
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

CLAUDE_DIR = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude"))
PROJECTS_DIR = CLAUDE_DIR / "projects"
HISTORY = CLAUDE_DIR / "history.jsonl"

log = logging.getLogger("monitor.usage")

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
    cache_read: int = 0
    messages: int = 0
    prompts: int = 0
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
    def __init__(self, interval_s: float = 10.0):
        self.interval_s = interval_s
        self.days: dict[str, DayUsage] = {}
        self._files: dict[str, _FileState] = {}
        self._history = _FileState()
        self._last_scan = 0.0
        self.last_scan_duration = 0.0
        self.scanned_once = False
        self.file_count = 0

    # ---- public --------------------------------------------------------------
    def due(self) -> bool:
        return time.time() - self._last_scan >= self.interval_s

    def scan(self) -> bool:
        """Incrementally read new bytes from every transcript. Returns True if changed."""
        t0 = time.time()
        self._last_scan = t0
        changed = False
        paths = glob.glob(str(PROJECTS_DIR / "*" / "*.jsonl")) + \
            glob.glob(str(PROJECTS_DIR / "*" / "*" / "subagents" / "*.jsonl"))
        self.file_count = len(paths)
        for p in paths:
            try:
                changed |= self._scan_transcript(p)
            except Exception as e:
                log.warning("usage scan failed for %s: %r", p, e)
        try:
            changed |= self._scan_history()
        except Exception as e:
            log.warning("history scan failed: %r", e)
        self.last_scan_duration = time.time() - t0
        if not self.scanned_once:
            log.info("usage: first scan of %d transcripts took %.2fs", len(paths), self.last_scan_duration)
        self.scanned_once = True
        return changed

    def report(self, n_days: int = 14) -> list[DayUsage]:
        """The last n_days of local days, oldest first, zero-filled."""
        today = datetime.now().date()
        out = []
        for i in range(n_days - 1, -1, -1):
            d = (today.fromordinal(today.toordinal() - i)).strftime("%Y-%m-%d")
            out.append(self.days.get(d) or DayUsage(date=d))
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

    def _read_new(self, path: str, st: _FileState) -> list[bytes]:
        try:
            s = os.stat(path)
        except FileNotFoundError:
            return []
        if s.st_size == st.size and s.st_mtime == st.mtime:
            return []
        st.size, st.mtime = s.st_size, s.st_mtime
        if s.st_size < st.offset:                    # truncated: start over
            st.offset, st.partial = 0, b""
        with open(path, "rb") as f:
            f.seek(st.offset)
            data = f.read()
            st.offset = f.tell()
        lines = (st.partial + data).split(b"\n")
        st.partial = lines.pop()
        return lines

    def _scan_transcript(self, path: str) -> bool:
        st = self._files.get(path)
        if st is None:
            st = self._files[path] = _FileState()
        lines = self._read_new(path, st)
        if not lines:
            return False
        changed = False
        for raw in lines:
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
                d.cache_read += cr
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

    def _scan_history(self) -> bool:
        lines = self._read_new(str(HISTORY), self._history)
        changed = False
        for raw in lines:
            try:
                row = json.loads(raw)
            except json.JSONDecodeError:
                continue
            ts = row.get("timestamp")
            if not isinstance(ts, (int, float)):
                continue
            day = datetime.fromtimestamp(ts / 1000).strftime("%Y-%m-%d")
            self._day(day).prompts += 1
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
              f"msgs {d.messages:>5}  prompts {d.prompts:>3}  sessions {d.session_count:>2}  "
              f"cost ${d.cost_usd:7.2f}")
    print("model split:", u.model_split(14)[:5])
