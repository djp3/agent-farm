"""
Passive collector for Claude Code instances on this machine.

Everything here is read-only. Claude Code already writes:
  ~/.claude/sessions/<pid>.json                      live registry (status busy|idle)
  ~/.claude/projects/<slug>/<sessionId>.jsonl        transcript
  ~/.claude/projects/<slug>/<sessionId>/subagents/   one .jsonl + .meta.json per subagent
  ~/.claude/teams/session-<id8>/config.json          team membership (lead + teammates)
iTerm2 ships `it2`, whose `session list --json` maps a tty to a tab.

No hooks are installed and nothing is sent to the instances.
"""
from __future__ import annotations

import glob
import json
import logging
import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass, field
from logging.handlers import RotatingFileHandler
from datetime import datetime, timezone
from pathlib import Path

from apppaths import log_dir
from typing import Optional

try:
    import psutil
except ImportError:  # pragma: no cover
    psutil = None

CLAUDE_DIR = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude"))
SESSIONS_DIR = CLAUDE_DIR / "sessions"
PROJECTS_DIR = CLAUDE_DIR / "projects"
TEAMS_DIR = CLAUDE_DIR / "teams"
IT2 = "/Applications/iTerm.app/Contents/Resources/utilities/it2"
ITERM_BUNDLE_ID = "com.googlecode.iterm2"
CLAUDE_APP_BUNDLE_ID = "com.anthropic.claudefordesktop"
LOG_DIR = log_dir()  # logs/ next to the code, or ~/Library/Logs/agent-farm in the app

log = logging.getLogger("monitor.collector")


class ErrorCounter(logging.Handler):
    """Counts WARNING+ records so the UI can show a terse badge. Keeps timestamps so the
    badge can reflect *recent* problems and clear when things have been quiet."""
    def __init__(self):
        super().__init__(level=logging.WARNING)
        self.count = 0
        self.last = ""
        self._when: list[float] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.count += 1
        self.last = record.getMessage()[:120]
        self._when.append(record.created)
        del self._when[:-500]

    def recent(self, window_s: float = 600.0) -> int:
        cutoff = time.time() - window_s
        return sum(1 for t in self._when if t >= cutoff)


def setup_logging(filename: str = "monitor.log", level: int = logging.INFO) -> Path:
    """Send everything under the 'monitor' logger to logs/<filename> (rotating, 1 MB x 3)."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    path = LOG_DIR / filename
    root = logging.getLogger("monitor")
    if not any(isinstance(h, RotatingFileHandler) for h in root.handlers):
        h = RotatingFileHandler(path, maxBytes=1_000_000, backupCount=3)
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
        root.addHandler(h)
    root.setLevel(level)
    root.propagate = False
    return path


class _Debounce:
    """Remember when a key was last logged so per-tick failures don't flood the log."""
    def __init__(self, every_s: float = 60.0):
        self.every_s = every_s
        self._seen: dict = {}

    def ok(self, key) -> bool:
        now = time.time()
        if now - self._seen.get(key, 0) >= self.every_s:
            self._seen[key] = now
            return True
        return False


_debounce = _Debounce()

# ---- git repo lookup, cached per folder ------------------------------------ #
GIT_TTL_S = 60.0
# A folder git cannot read yet (macOS holds the call while it asks the user for Files and
# Folders access, so it times out) is retried rarely and mentioned in the log rarely.
GIT_NO_ACCESS_TTL_S = 300.0
_git_cache: dict[str, tuple[float, dict, float]] = {}
_git_debounce = _Debounce(every_s=900)


def _git(cwd: str, *args: str) -> str:
    r = subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True, timeout=2)
    return r.stdout.strip() if r.returncode == 0 else ""


def git_info(cwd: str) -> dict:
    """{'root', 'repo', 'remote', 'branch'} for the repository containing cwd, or {} if none.
    Cached for GIT_TTL_S so the per-second collector never shells out repeatedly."""
    if not cwd or not os.path.isdir(cwd):
        return {}
    now = time.time()
    hit = _git_cache.get(cwd)
    if hit and now - hit[0] < hit[2]:
        return hit[1]
    info: dict = {}
    ttl = GIT_TTL_S
    try:
        root = _git(cwd, "rev-parse", "--show-toplevel")
        if root:
            remote = _git(cwd, "remote", "get-url", "origin")
            if remote.endswith(".git"):
                remote = remote[:-4]
            # A linked worktree has its own git dir but shares the main checkout's
            # common dir; when the two differ, this is a worktree of that main repo.
            git_dir = os.path.abspath(os.path.join(cwd, _git(cwd, "rev-parse", "--git-dir") or ".git"))
            common = os.path.abspath(os.path.join(cwd, _git(cwd, "rev-parse", "--git-common-dir") or ".git"))
            worktree = os.path.normpath(git_dir) != os.path.normpath(common)
            main_root = os.path.dirname(common) if worktree else ""
            info = {
                "root": root,
                "repo": os.path.basename(root),
                "remote": remote,
                "branch": _git(cwd, "rev-parse", "--abbrev-ref", "HEAD"),
                "worktree": worktree,
                "main_repo": os.path.basename(main_root) if worktree else "",
                "main_root": main_root,
                "claude_worktree": worktree and "/.claude/worktrees/" in root + "/",
            }
    except subprocess.TimeoutExpired:
        info, ttl = {"error": "no access"}, GIT_NO_ACCESS_TTL_S
        if _git_debounce.ok(("git-access", cwd)):
            log.warning("git lookup timed out in %s: macOS is probably waiting for agent-farm to be "
                        "allowed under System Settings > Privacy & Security > Files and Folders", cwd)
    except Exception as e:
        if _debounce.ok(("git", cwd)):
            log.warning("git lookup failed in %s: %r", cwd, e)
    _git_cache[cwd] = (now, info, ttl)
    return info

# How long a subagent transcript may sit unchanged and still count as running.
SUBAGENT_RUNNING_GRACE_S = 90
# A subagent whose transcript ends mid tool-call but has been silent this long is 'stale'.
SUBAGENT_STALE_S = 15 * 60
_TEAMMATE_RE = re.compile(r'<teammate-message[^>]*\bteammate_id="([^"]+)"')
# On first open, only read this much from the end of a large transcript.
FIRST_READ_TAIL_BYTES = 3 * 1024 * 1024


def _parse_ts(s: Optional[str]) -> Optional[float]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _first_line(s: str, n: int = 160) -> str:
    s = (s or "").strip().replace("\r", "")
    s = s.split("\n", 1)[0]
    return (s[: n - 1] + "…") if len(s) > n else s


def _tool_summary(name: str, inp: dict) -> str:
    """One line describing a tool call, preferring the model's own description."""
    if not isinstance(inp, dict):
        return name
    for key in ("description", "pattern", "command", "file_path", "query", "prompt", "url", "skill"):
        v = inp.get(key)
        if isinstance(v, str) and v.strip():
            return _first_line(v, 120)
    return name


# --------------------------------------------------------------------------- #
# Transcript tailing
# --------------------------------------------------------------------------- #
@dataclass
class TranscriptState:
    path: Path
    offset: int = 0
    partial: bytes = b""              # bytes, so a UTF-8 char split across reads survives
    size: int = 0
    mtime: float = 0.0

    title: str = ""
    git_branch: str = ""
    cwd: str = ""
    model: str = ""
    effort: str = ""                   # effort level on the latest assistant row (high, xhigh, …)
    last_prompt: str = ""
    last_prompt_ts: Optional[float] = None
    last_text: str = ""
    last_text_ts: Optional[float] = None
    last_ts: Optional[float] = None
    last_row_kind: str = ""            # 'text' | 'tool_use' | 'tool_result' | 'prompt' | other
    stop_reason: str = ""
    pending_tools: dict = field(default_factory=dict)   # tool_use_id -> (name, summary, ts)
    permission_pending: bool = False
    permission_ts: Optional[float] = None
    away_summary: str = ""
    context_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    queued: list = field(default_factory=list)          # [{"content", "ts"}] prompts typed while busy
    spawns: dict = field(default_factory=dict)          # agent_id -> {name, description, ts}
    finished_agents: set = field(default_factory=set)   # teammates whose final message arrived
    rows_seen: int = 0
    bad_rows: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    # ---- incremental read -------------------------------------------------
    def poll(self) -> bool:
        """Read any new bytes. Returns True if something changed."""
        try:
            st = self.path.stat()
        except FileNotFoundError:
            return False
        if st.st_size == self.size and st.st_mtime == self.mtime:
            return False
        self.mtime = st.st_mtime
        if st.st_size < self.offset:            # truncated/rotated: start over
            self.offset, self.partial = 0, b""
        try:
            with self.path.open("rb") as f:
                if self.offset == 0 and st.st_size > FIRST_READ_TAIL_BYTES:
                    f.seek(st.st_size - FIRST_READ_TAIL_BYTES)
                    f.readline()                 # drop the partial first line
                    self.offset = f.tell()
                else:
                    f.seek(self.offset)
                data = f.read()
                self.offset = f.tell()
        except OSError:                          # removed between stat() and open()
            return False
        self.size = st.st_size
        lines = (self.partial + data).split(b"\n")
        self.partial = lines.pop()               # b'' or an incomplete trailing line
        before = self.bad_rows
        with self.lock:
            for raw in lines:
                if not raw.strip():
                    continue
                try:
                    self._ingest(raw.decode("utf-8", errors="replace"))
                except Exception as e:           # one malformed row must not poison the rest
                    self.bad_rows += 1
                    if _debounce.ok(("ingest", str(self.path))):
                        log.warning("bad transcript row in %s: %r :: %.200s", self.path.name, e, raw)
        if self.bad_rows > before and _debounce.ok(("badrows", str(self.path))):
            log.warning("%d malformed row(s) skipped in %s (total %d)",
                        self.bad_rows - before, self.path.name, self.bad_rows)
        return True

    def _ingest(self, line: str) -> None:
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            self.bad_rows += 1
            return
        if not isinstance(row, dict):
            self.bad_rows += 1
            return
        self.rows_seen += 1
        t = row.get("type")
        ts = _parse_ts(row.get("timestamp"))
        if ts:
            self.last_ts = ts
        if row.get("gitBranch"):
            self.git_branch = row["gitBranch"]
        if row.get("cwd"):
            self.cwd = row["cwd"]

        if t == "custom-title":
            self.title = row.get("customTitle") or self.title
        elif t == "last-prompt":
            lp = row.get("lastPrompt") or ""
            if lp and not self._note_teammate_messages(lp) and not lp.startswith("<"):
                self.last_prompt = lp
        elif t == "user":
            self._ingest_user(row, ts)
        elif t == "assistant":
            self._ingest_assistant(row, ts)
        elif t == "attachment":
            att = row.get("attachment") or {}
            ev = att.get("hookEvent") or ""
            if ev == "PermissionRequest":
                self.permission_pending, self.permission_ts = True, ts
            elif ev in ("PostToolUse", "UserPromptSubmit", "Stop", "SessionEnd"):
                self.permission_pending = False
        elif t == "system":
            if row.get("subtype") == "away_summary":
                self.away_summary = row.get("content") or ""
        elif t == "cost-state":
            try:
                self.cost_usd = float(row.get("totalCostUSD") or 0)
            except (TypeError, ValueError):
                pass
        elif t == "queue-operation":
            # enqueue: user typed while busy. dequeue: oldest item starts a turn (no content
            # in the row). remove: a specific item was absorbed mid-turn or deleted.
            # popAll: everything was consumed at once.
            op = row.get("operation")
            c = row.get("content") or ""
            if op == "enqueue":
                self.queued.append({"content": c, "ts": ts})
            elif op == "dequeue":
                if self.queued:
                    self.queued.pop(0)
            elif op == "remove":
                idx = next((k for k, q in enumerate(self.queued) if q["content"] == c), 0 if self.queued else None)
                if idx is not None:
                    self.queued.pop(idx)
            elif op == "popAll":
                self.queued.clear()

    def _ingest_user(self, row: dict, ts: Optional[float]) -> None:
        msg = row.get("message") or {}
        content = msg.get("content")
        if isinstance(content, str):
            relay = self._note_teammate_messages(content)
            if not row.get("isMeta") and not relay and not content.startswith("<"):
                self.last_prompt, self.last_prompt_ts = content, ts
                self.last_row_kind = "prompt"
                self.permission_pending = False
            return
        if not isinstance(content, list):
            return
        for c in content:
            if not isinstance(c, dict):
                continue
            ct = c.get("type")
            if ct == "tool_result":
                self.pending_tools.pop(c.get("tool_use_id"), None)
                self.last_row_kind = "tool_result"
                self.permission_pending = False
            elif ct == "text" and not row.get("isMeta"):
                txt = c.get("text") or ""
                relay = self._note_teammate_messages(txt)
                if txt and not relay and not txt.startswith("<"):
                    self.last_prompt, self.last_prompt_ts = txt, ts
                    self.last_row_kind = "prompt"
        tur = row.get("toolUseResult")
        if isinstance(tur, dict) and tur.get("status") == "teammate_spawned":
            aid = tur.get("agent_id") or tur.get("teammate_id") or ""
            self.spawns[aid.split("@")[0]] = {
                "name": tur.get("name") or "",
                "description": "",
                "ts": ts,
            }

    def _note_teammate_messages(self, text: str) -> bool:
        """A <teammate-message teammate_id="X"> in the head's transcript means X finished.
        Returns True if the text is such a relay (so it is not shown as the user's prompt)."""
        if text and "teammate-message" in text:
            for name in _TEAMMATE_RE.findall(text):
                self.finished_agents.add(name)
            return True
        return False

    def _ingest_assistant(self, row: dict, ts: Optional[float]) -> None:
        msg = row.get("message") or {}
        if not isinstance(msg, dict):
            return
        if msg.get("model"):
            self.model = msg["model"]
        eff = row.get("perTurnEffort") or row.get("effort")
        if isinstance(eff, str) and eff:
            self.effort = eff
        if msg.get("stop_reason"):
            self.stop_reason = msg["stop_reason"]
        usage = msg.get("usage") or {}
        if usage:
            self.context_tokens = (
                int(usage.get("input_tokens") or 0)
                + int(usage.get("cache_read_input_tokens") or 0)
                + int(usage.get("cache_creation_input_tokens") or 0)
            )
            self.output_tokens = int(usage.get("output_tokens") or 0)
        content = msg.get("content")
        if not isinstance(content, list):            # a plain-string assistant message
            if isinstance(content, str) and content.strip():
                self.last_text, self.last_text_ts = content, ts
                self.last_row_kind = "text"
            return
        for c in content:
            if not isinstance(c, dict):
                continue
            ct = c.get("type")
            if ct == "text":
                txt = c.get("text") or ""
                if txt.strip():
                    self.last_text, self.last_text_ts = txt, ts
                    self.last_row_kind = "text"
            elif ct == "tool_use":
                name = c.get("name") or "?"
                inp = c.get("input") or {}
                self.pending_tools[c.get("id")] = (name, _tool_summary(name, inp), ts)
                self.last_row_kind = "tool_use"
                if name in ("Agent", "Task") and isinstance(inp, dict):
                    nm = inp.get("name") or ""
                    if nm:
                        self.spawns.setdefault(nm, {}).update(
                            {"name": nm, "description": inp.get("description") or "", "ts": ts}
                        )

    # ---- derived ----------------------------------------------------------
    @property
    def current_tool(self) -> Optional[tuple]:
        with self.lock:                          # the worker thread mutates pending_tools
            vals = list(self.pending_tools.values())
        if not vals:
            return None
        return max(vals, key=lambda v: v[2] or 0)


_tails: dict[str, TranscriptState] = {}


def tail_for(path: Path) -> TranscriptState:
    key = str(path)
    st = _tails.get(key)
    if st is None:
        st = _tails[key] = TranscriptState(path=path)
    st.poll()
    return st


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #
@dataclass
class Agent:
    """A subagent / teammate under a head session."""
    agent_id: str
    name: str
    agent_type: str = ""
    description: str = ""
    model: str = ""
    color: str = ""
    status: str = "unknown"          # running | dormant | stale | starting | archived
    archived: bool = False           # from an earlier run of this session: a log, not a live teammate
    transcript: Optional[TranscriptState] = None
    started_ts: Optional[float] = None
    last_activity: Optional[float] = None

    @property
    def activity(self) -> str:
        t = self.transcript
        if not t:
            return self.description
        if self.status == "running" and t.current_tool:
            name, summ, _ = t.current_tool
            return f"{name}: {summ}"
        if t.last_text:
            return _first_line(t.last_text)
        return self.description

    @property
    def last_ts(self) -> Optional[float]:
        return self.transcript.last_ts if self.transcript else None


@dataclass
class Instance:
    """A head Claude Code process (one per terminal)."""
    pid: int
    session_id: str = ""
    name: str = ""
    cwd: str = ""
    kind: str = ""
    entrypoint: str = ""             # "cli" for a terminal, "claude-desktop" for the Claude app
    host_session_id: str = ""        # the desktop app's own id for the conversation
    version: str = ""
    started_at: Optional[float] = None
    status: str = "unknown"          # busy | idle | waiting | dead | unknown
    status_detail: str = ""
    status_since: Optional[float] = None
    alive: bool = True
    tty: str = ""
    cpu: float = 0.0
    rss_mb: float = 0.0
    iterm_session: str = ""
    iterm_title: str = ""
    iterm_tab: str = ""
    iterm_window: str = ""
    transcript: Optional[TranscriptState] = None
    agents: list[Agent] = field(default_factory=list)
    git: dict = field(default_factory=dict)      # from git_info(cwd); {} when not a repository

    @property
    def is_app(self) -> bool:
        """Running inside the Claude desktop app rather than a terminal."""
        return self.entrypoint == "claude-desktop"

    @property
    def project(self) -> str:
        return Path(self.cwd).name if self.cwd else ""

    @property
    def branch(self) -> str:
        b = (self.transcript.git_branch if self.transcript else "") or self.git.get("branch", "")
        return "" if b == "HEAD" else b          # "HEAD" means "not a git repo" here

    @property
    def activity(self) -> str:
        t = self.transcript
        if not t:
            return ""
        if self.status == "waiting":
            return self.status_detail
        if self.status == "busy":
            if t.current_tool:
                name, summ, _ = t.current_tool
                return f"{name}: {summ}"
            return "thinking…" if not t.last_text else _first_line(t.last_text)
        if t.last_text:
            return _first_line(t.last_text)
        return ""

    @property
    def last_prompt(self) -> str:
        return _first_line(self.transcript.last_prompt, 200) if self.transcript else ""

    @property
    def model(self) -> str:
        return self.transcript.model if self.transcript else ""


# --------------------------------------------------------------------------- #
# iTerm2 mapping
# --------------------------------------------------------------------------- #
class ITerm2Map:
    def __init__(self, refresh_s: float = 5.0):
        self.refresh_s = refresh_s
        self._last = 0.0
        self.by_tty: dict[str, dict] = {}
        self.available = os.path.exists(IT2)
        self._failures = 0                      # consecutive `session list` failures

    def refresh(self, force: bool = False) -> None:
        if not self.available:
            return
        now = time.time()
        if not force and now - self._last < self.refresh_s:
            return
        self._last = now
        try:
            r = subprocess.run(
                [IT2, "session", "list", "--json"],
                capture_output=True, text=True, timeout=8,
            )
            if r.returncode != 0:
                raise RuntimeError(f"rc={r.returncode} stderr={r.stderr.strip()[:300]}")
            rows = json.loads(r.stdout or "[]")
        except Exception as e:
            # The previous tab map stays in use, so a single slow answer from iTerm2 is
            # not worth a warning; a run of them is.
            self._failures += 1
            if self._failures >= 3:
                if _debounce.ok("it2-list"):
                    log.warning("it2 session list failed %d times in a row: %r", self._failures, e)
            else:
                log.info("it2 session list slow/failed (%d): %r — keeping the previous tab map",
                         self._failures, e)
            return
        self._failures = 0
        m = {}
        for r in rows:
            tty = (r.get("tty") or "").replace("\\", "")
            if tty:
                m[tty] = r
        self.by_tty = m

    def lookup(self, tty: str) -> dict:
        if not tty:
            return {}
        if not tty.startswith("/dev/"):
            tty = "/dev/" + tty
        return self.by_tty.get(tty, {})

    @staticmethod
    @staticmethod
    def activate(bundle_id: str, label: str = "") -> tuple[bool, str]:
        """Bring an application to the front through Launch Services (`open -b`). Unlike an
        AppleScript `activate` this needs no Automation permission, which matters once the
        monitor runs as its own app rather than inside iTerm2."""
        try:
            r = subprocess.run(["open", "-b", bundle_id], capture_output=True, text=True, timeout=5)
        except Exception as e:
            log.exception("focus %s: open -b %s failed to run", label, bundle_id)
            return False, repr(e)
        if r.returncode == 0:
            log.info("focus %s: activated %s", label, bundle_id)
            return True, ""
        msg = (r.stderr or r.stdout).strip()[:300]
        log.warning("focus %s: open -b %s rc=%d :: %s", label, bundle_id, r.returncode, msg)
        return False, msg or f"open exited {r.returncode}"

    def focus_app(self, label: str = "") -> tuple[bool, str]:
        """Bring the Claude desktop app to the front (sessions that run inside it have no
        terminal to focus). The app exposes no way to select a particular conversation."""
        return self.activate(CLAUDE_APP_BUNDLE_ID, label)

    def focus(self, session_id: str, label: str = "") -> tuple[bool, str]:
        """Bring an iTerm2 session to the front. `it2 session focus <session-id>` is positional."""
        if not self.available:
            log.warning("focus %s: it2 not found at %s", label, IT2)
            return False, "it2 not installed"
        if not session_id:
            log.warning("focus %s: no iTerm2 session id mapped", label)
            return False, "no iTerm2 session mapped"
        cmd = [IT2, "session", "focus", session_id]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=3)
        except Exception as e:
            log.exception("focus %s: %s failed to run", label, " ".join(cmd))
            return False, repr(e)
        if r.returncode == 0:
            log.info("focus %s: ok (session %s)", label, session_id)
            # `it2 session focus` selects the tab and window inside iTerm2 but cannot bring
            # iTerm2 in front of another app; from a terminal that never showed because
            # iTerm2 was already frontmost. Activate it explicitly.
            return self.activate(ITERM_BUNDLE_ID, label)
        msg = (r.stderr or r.stdout).strip()[:300]
        log.warning("focus %s: rc=%d cmd=%s :: %s", label, r.returncode, " ".join(cmd), msg)
        return False, msg or f"it2 exited {r.returncode}"


# --------------------------------------------------------------------------- #
# Collector
# --------------------------------------------------------------------------- #
class Collector:
    def __init__(self):
        self.iterm = ITerm2Map()
        self._transcript_paths: dict[str, Path] = {}

    # ---- helpers ----------------------------------------------------------
    @staticmethod
    def _pid_alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True

    def _find_transcript(self, session_id: str, cwd: str) -> Optional[Path]:
        p = self._transcript_paths.get(session_id)
        if p and p.exists():
            return p
        if cwd:
            slug = re.sub(r"[^A-Za-z0-9]", "-", cwd)
            cand = PROJECTS_DIR / slug / f"{session_id}.jsonl"
            if cand.exists():
                self._transcript_paths[session_id] = cand
                return cand
        hits = glob.glob(str(PROJECTS_DIR / "*" / f"{session_id}.jsonl"))
        if hits:
            self._transcript_paths[session_id] = Path(hits[0])
            return Path(hits[0])
        return None

    @staticmethod
    def _ttys_for(pids: list[int]) -> dict[int, str]:
        """pid -> /dev/ttysNNN via one `ps` call. (psutil caches the tty table at first
        use, so terminals opened after the monitor started would come back empty.)"""
        if not pids:
            return {}
        try:
            out = subprocess.run(
                ["ps", "-o", "pid=,tty=", "-p", ",".join(str(p) for p in pids)],
                capture_output=True, text=True, timeout=2,
            ).stdout
        except Exception as e:
            if _debounce.ok("ps-tty"):
                log.warning("ps tty lookup failed: %r", e)
            return {}
        m: dict[int, str] = {}
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[0].isdigit() and parts[1] not in ("??", "-"):
                m[int(parts[0])] = "/dev/" + parts[1]
        return m

    def _proc_info(self, inst: Instance) -> None:
        if psutil is None:
            return
        try:
            p = psutil.Process(inst.pid)
            inst.cpu = p.cpu_percent(interval=None)
            inst.rss_mb = p.memory_info().rss / (1024 * 1024)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

    def _agents_for(self, inst: Instance, transcript: Optional[Path]) -> list[Agent]:
        agents: list[Agent] = []
        if transcript is None:
            return agents
        sub_dir = transcript.parent / inst.session_id / "subagents"
        now = time.time()
        proc_start = inst.started_at or 0.0      # the current process; earlier runs' agents are logs
        if sub_dir.is_dir():
            for meta_path in sorted(sub_dir.glob("agent-*.meta.json")):
                jsonl = meta_path.with_name(meta_path.name.replace(".meta.json", ".jsonl"))
                try:
                    meta = json.loads(meta_path.read_text())
                except Exception:
                    meta = {}
                agent_id = meta_path.name[len("agent-"):-len(".meta.json")]
                tail = tail_for(jsonl) if jsonl.exists() else None
                a = Agent(
                    agent_id=agent_id,
                    name=meta.get("name") or meta.get("agentType") or agent_id,   # unnamed agents show their type
                    agent_type=meta.get("agentType") or "",
                    description=meta.get("description") or "",
                    model=meta.get("model") or "",
                    color=meta.get("color") or "",
                    transcript=tail,
                )
                try:
                    a.started_ts = meta_path.stat().st_mtime
                except FileNotFoundError:
                    pass
                a.last_activity = max(tail.mtime if tail else 0.0, a.started_ts or 0.0) or None
                if inst.transcript:
                    sp = inst.transcript.spawns.get(a.name) or {}
                    a.description = a.description or sp.get("description", "")
                # Teammates live inside the head process. Anything last active before this
                # process started belongs to an earlier run and cannot be woken again.
                a.archived = (not inst.alive) or bool(
                    proc_start and a.last_activity and a.last_activity < proc_start)
                if a.archived:
                    a.status = "archived"
                elif tail is None:
                    a.status = "starting"
                elif inst.transcript and a.name in inst.transcript.finished_agents:
                    a.status = "dormant"         # finished; idle on its mailbox, reusable by the head
                else:
                    age = now - tail.mtime
                    mid_work = tail.last_row_kind in ("tool_use", "tool_result", "prompt")
                    if age < SUBAGENT_RUNNING_GRACE_S:
                        a.status = "running"
                    elif mid_work and age < SUBAGENT_STALE_S:
                        a.status = "running"     # a long tool call can be silent for minutes
                    elif mid_work:
                        a.status = "stale"       # silent too long mid tool-call: killed or crashed?
                    else:
                        a.status = "dormant"
                agents.append(a)

        # Team config: members other than the lead (present when a team is formed).
        team_cfg = TEAMS_DIR / f"session-{inst.session_id[:8]}" / "config.json"
        if team_cfg.exists():
            try:
                cfg = json.loads(team_cfg.read_text())
                lead = cfg.get("leadAgentId")
                known = {a.name for a in agents}
                for m in cfg.get("members") or []:
                    if m.get("agentId") == lead or m.get("name") in known:
                        continue
                    agents.append(Agent(
                        agent_id=m.get("agentId") or m.get("name") or "?",
                        name=m.get("name") or "?",
                        agent_type=m.get("agentType") or "",
                        description=f"teammate ({m.get('backendType', '')})",
                        status="running" if inst.alive else "archived",
                        archived=not inst.alive,
                        started_ts=(m.get("joinedAt") or 0) / 1000 or None,
                    ))
            except Exception:
                pass
        agents.sort(key=lambda a: a.started_ts or 0)
        return agents

    # ---- main entry -------------------------------------------------------
    def snapshot(self) -> list[Instance]:
        self.iterm.refresh()
        instances: dict[int, Instance] = {}
        now = time.time()

        for f in sorted(SESSIONS_DIR.glob("*.json")):
            try:
                reg = json.loads(f.read_text())
                pid = int(reg.get("pid") or f.stem.split(".")[0])
            except Exception as e:
                if _debounce.ok(("registry", f.name)):
                    log.warning("unreadable registry file %s: %r", f.name, e)
                continue
            inst = Instance(
                pid=pid,
                session_id=reg.get("sessionId") or "",
                name=reg.get("name") or "",
                cwd=reg.get("cwd") or "",
                kind=reg.get("kind") or "",
                entrypoint=reg.get("entrypoint") or "",
                host_session_id=reg.get("hostSessionId") or "",
                version=reg.get("version") or "",
                started_at=(reg.get("startedAt") or 0) / 1000 or None,
                status=reg.get("status") or "unknown",
                status_since=(reg.get("statusUpdatedAt") or 0) / 1000 or None,
            )
            inst.alive = self._pid_alive(pid)
            if not inst.alive:
                inst.status = "dead"
            instances[pid] = inst

        # Claude processes not in the registry (older builds, odd entrypoints).
        if psutil is not None:
            for p in psutil.process_iter(["pid", "name", "cmdline", "create_time", "cwd"]):
                try:
                    if p.info["name"] != "claude" or p.info["pid"] in instances:
                        continue
                    cmd = p.info["cmdline"] or []
                    inst = Instance(pid=p.info["pid"], kind="unregistered",
                                    started_at=p.info["create_time"], cwd=p.info["cwd"] or "")
                    if "-n" in cmd:
                        inst.name = cmd[cmd.index("-n") + 1] if cmd.index("-n") + 1 < len(cmd) else ""
                    instances[inst.pid] = inst
                except (psutil.NoSuchProcess, psutil.AccessDenied, ValueError):
                    continue

        ttys = self._ttys_for([i.pid for i in instances.values() if i.alive])
        for inst in instances.values():
            try:
                self._fill_instance(inst, ttys, now)
            except Exception as e:               # one broken instance must not blank the others
                inst.status_detail = "collector error (see logs/monitor.log)"
                if _debounce.ok(("instance", inst.pid)):
                    log.exception("collecting pid=%s name=%r failed: %r", inst.pid, inst.name, e)

        out = list(instances.values())
        out.sort(key=lambda i: (not i.alive, i.started_at or 0))
        return out

    def focus_instance(self, inst: Instance, label: str = "") -> tuple[bool, str]:
        """Bring whatever hosts this session to the front: its iTerm2 tab, or the Claude app."""
        if inst.is_app:
            return self.iterm.focus_app(label)
        return self.iterm.focus(inst.iterm_session, label)

    def _fill_instance(self, inst: Instance, ttys: dict[int, str], now: float) -> None:
        if inst.alive:
            self._proc_info(inst)
            inst.tty = ttys.get(inst.pid, "")
            it = self.iterm.lookup(inst.tty)
            inst.iterm_session = it.get("id", "")
            inst.iterm_title = it.get("title") or it.get("name") or ""
            inst.iterm_tab = str(it.get("tab_id", ""))
            inst.iterm_window = it.get("window_id", "")
        tpath = self._find_transcript(inst.session_id, inst.cwd) if inst.session_id else None
        if tpath:
            inst.transcript = tail_for(tpath)
            if not inst.name and inst.transcript.title:
                inst.name = inst.transcript.title
            if not inst.cwd:
                inst.cwd = inst.transcript.cwd
        if inst.alive and inst.cwd:
            inst.git = git_info(inst.cwd)
        self._refine_status(inst, now)
        inst.agents = self._agents_for(inst, tpath)

    @staticmethod
    def _refine_status(inst: Instance, now: float) -> None:
        """Turn busy/idle into busy/idle/waiting using transcript evidence."""
        t = inst.transcript
        if not inst.alive or t is None:
            return
        if t.permission_pending:
            inst.status, inst.status_detail = "waiting", "permission prompt"
            inst.status_since = t.permission_ts or inst.status_since
            return
        tool = t.current_tool
        if tool and tool[0] in ("AskUserQuestion", "ExitPlanMode"):
            inst.status, inst.status_detail = "waiting", f"{tool[0]}: {tool[1]}"
            inst.status_since = tool[2] or inst.status_since
            return
        inst.status_detail = ""


# --------------------------------------------------------------------------- #
# Formatting helpers shared with the TUI
# --------------------------------------------------------------------------- #
def fmt_age(ts: Optional[float], now: Optional[float] = None) -> str:
    if not ts:
        return "—"
    d = int((now or time.time()) - ts)
    if d < 0:
        d = 0
    if d < 60:
        return f"{d}s"
    if d < 3600:
        return f"{d // 60}m{d % 60:02d}s"
    if d < 86400:
        return f"{d // 3600}h{(d % 3600) // 60:02d}m"
    return f"{d // 86400}d{(d % 86400) // 3600}h"


def fmt_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1000:
        return f"{n / 1000:.0f}k"
    return str(n)


STATUS_GLYPH = {
    "busy": "●", "idle": "○", "waiting": "◐", "dead": "✕", "unknown": "?",
    "running": "●", "dormant": "✓", "archived": "▫", "starting": "◌", "stale": "◍",
}


if __name__ == "__main__":
    c = Collector()
    for inst in c.snapshot():
        print(f"{STATUS_GLYPH.get(inst.status, '?')} [{inst.pid}] {inst.name or '(unnamed)'}  "
              f"{inst.project}{'@' + inst.branch if inst.branch else ''}  {inst.status} {fmt_age(inst.status_since)}  "
              f"ctx={fmt_tokens(inst.transcript.context_tokens) if inst.transcript else '?'}  "
              f"tty={inst.tty} iterm={inst.iterm_title!r}")
        print(f"    prompt : {inst.last_prompt}")
        print(f"    doing  : {inst.activity}")
        for a in inst.agents:
            print(f"    └ {STATUS_GLYPH.get(a.status, '?')} {a.name} [{a.agent_type}] {a.status}: {a.activity}")
