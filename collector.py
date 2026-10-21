"""
Passive collector for Claude Code instances on this machine.

Everything here is read-only. Claude Code already writes:
  ~/.claude/sessions/<pid>.json                      live registry (status busy|idle)
  ~/.claude/projects/<slug>/<sessionId>.jsonl        transcript
  ~/.claude/projects/<slug>/<sessionId>/subagents/   one .jsonl + .meta.json per subagent
  ~/.claude/teams/session-<id8>/config.json          team membership (lead + teammates)
iTerm2 ships `it2`, whose `session list --json` maps a tty to a tab, and the process table
shows the shells Claude Code is running commands in, as children of the session's process.

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
from datetime import datetime
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

log = logging.getLogger("agent_farm.collector")


class ErrorCounter(logging.Handler):
    """Counts WARNING+ records so the UI can show a terse badge. Keeps timestamps so the
    badge can reflect *recent* problems and clear when things have been quiet."""
    def __init__(self):
        super().__init__(level=logging.WARNING)
        self.count = 0
        self._when: list[float] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.count += 1
        self._when.append(record.created)
        del self._when[:-500]

    def recent(self, window_s: float = 600.0) -> int:
        cutoff = time.time() - window_s
        return sum(1 for t in self._when if t >= cutoff)


def setup_logging(filename: str = "agent-farm.log", level: int = logging.INFO) -> Path:
    """Send everything under the 'monitor' logger to logs/<filename> (rotating, 1 MB x 3)."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    path = LOG_DIR / filename
    root = logging.getLogger("agent_farm")
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


# Helper programs by absolute path, so nothing is resolved through a PATH entry that a
# user-level process could write to. git is the exception by design: Homebrew's build is
# preferred (located by asking brew for its prefix), then Apple's, which exists on every
# Mac but is only a stub that pops up the developer-tools installer unless the command
# line tools are present, so it is used only when xcode-select confirms they are.
PS = "/bin/ps"
OPEN = "/usr/bin/open"
XCODE_SELECT = "/usr/bin/xcode-select"
APPLE_GIT = "/usr/bin/git"
BREW_CANDIDATES = ("/opt/homebrew/bin/brew", "/usr/local/bin/brew")   # Apple silicon, Intel
GIT_INSTALL_HINT = "run  xcode-select --install  for Apple's git, or  brew install git  for Homebrew's"
_git_path: Optional[str] = None          # resolved once; "" means no usable git


def _run_quiet(cmd: list[str], timeout: float = 5) -> str:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except Exception:
        return ""
    return r.stdout.strip() if r.returncode == 0 else ""


def find_git() -> str:
    """Homebrew's git, else Apple's when the developer tools are installed, else ''."""
    for brew in BREW_CANDIDATES:
        if os.path.exists(brew):
            prefix = _run_quiet([brew, "--prefix"])
            cand = os.path.join(prefix, "bin", "git") if prefix else ""
            if cand and os.access(cand, os.X_OK):
                return cand
    if _run_quiet([XCODE_SELECT, "-p"]) and os.access(APPLE_GIT, os.X_OK):
        return APPLE_GIT
    return ""


def git_path() -> str:
    global _git_path
    if _git_path is None:
        _git_path = find_git()
        log.info("git: %s", _git_path or "not found")
    return _git_path


def _git(cwd: str, *args: str) -> str:
    r = subprocess.run([git_path(), "-C", cwd, *args], capture_output=True, text=True, timeout=2)
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
    if not git_path():                    # nothing to run; the detail pane says what to install
        _git_cache[cwd] = (now, {"error": "no git"}, GIT_NO_ACCESS_TTL_S)
        return _git_cache[cwd][1]
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
# Claude Code runs every command it is asked for (Bash tool, monitors) as a child of its own
# process: `<shell> -c "source …/shell-snapshots/snapshot-… && … eval '<command>' …"`. The
# snapshot path tells those shells from the MCP servers and helpers it also starts.
SHELL_MARK = "/shell-snapshots/snapshot-"
_EVAL_RE = re.compile(r"\beval '((?:[^']|'\"'\"')*)'")
COMMANDS_KEPT = 200        # recent commands per transcript, to name the shells running them
# A command issued this long after its shell started is a later run of the same command.
SHELL_ISSUE_SLACK_S = 10
# On first open, only read this much from the end of a large transcript.
FIRST_READ_TAIL_BYTES = 3 * 1024 * 1024


def _parse_ts(s: Optional[str]) -> Optional[float]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


# C0 controls except newline and tab, DEL, C1 controls, and the Unicode bidi controls that
# can make text render misleadingly. Everything shown comes from files another program
# wrote, and the UI is a terminal: an assistant message containing ESC ] 52 ; ... would
# otherwise travel through Textual untouched and rewrite the clipboard.
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f\x80-\x9f\u2028\u2029\u202a-\u202e\u2066-\u2069]")


def clean_text(s) -> str:
    """Text from a transcript, registry, meta file or iTerm2, made safe to put on screen."""
    if not s:
        return ""
    if not isinstance(s, str):
        s = str(s)
    return _CONTROL_RE.sub("", s)


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
    pending_tools: dict = field(default_factory=dict)   # tool_use_id -> (name, summary, ts)
    permission_pending: bool = False
    permission_ts: Optional[float] = None
    away_summary: str = ""
    context_tokens: int = 0
    cost_usd: float = 0.0
    queued: list = field(default_factory=list)          # [{"content", "ts"}] prompts typed while busy
    spawns: dict = field(default_factory=dict)          # agent_id -> {name, description, ts}
    finished_agents: dict = field(default_factory=dict) # teammate name -> when its last reply arrived
    commands: dict = field(default_factory=dict)        # command -> (summary, ts), latest issue last
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
                        # Never the row itself: transcripts carry prompts and pasted text.
                        log.warning("bad transcript row in %s: %r (%d bytes)", self.path.name, e, len(raw))
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
        t = row.get("type")
        ts = _parse_ts(row.get("timestamp"))
        if ts:
            self.last_ts = ts
        if row.get("gitBranch"):
            self.git_branch = row["gitBranch"]
        if row.get("cwd"):
            self.cwd = row["cwd"]

        if t == "custom-title":
            self.title = clean_text(row.get("customTitle")) or self.title
        elif t == "last-prompt":
            lp = clean_text(row.get("lastPrompt"))
            if lp and not self._note_teammate_messages(lp, ts) and not lp.startswith("<"):
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
                self.away_summary = clean_text(row.get("content"))
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
            c = clean_text(row.get("content"))
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
            relay = self._note_teammate_messages(content, ts)
            if not row.get("isMeta") and not relay and not content.startswith("<"):
                self.last_prompt, self.last_prompt_ts = clean_text(content), ts
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
                relay = self._note_teammate_messages(txt, ts)
                if txt and not relay and not txt.startswith("<"):
                    self.last_prompt, self.last_prompt_ts = clean_text(txt), ts
                    self.last_row_kind = "prompt"
        tur = row.get("toolUseResult")
        if isinstance(tur, dict) and tur.get("status") == "teammate_spawned":
            aid = tur.get("agent_id") or tur.get("teammate_id") or ""
            self.spawns[aid.split("@")[0]] = {
                "name": clean_text(tur.get("name")),
                "description": "",
                "ts": ts,
            }

    def _note_teammate_messages(self, text: str, ts: Optional[float] = None) -> bool:
        """A <teammate-message teammate_id="X"> in the head's transcript means X reported
        back at that moment. Teammates stay resident and can be given more work, so the
        time is kept, not just the fact: X counts as finished only until its own transcript
        changes again. Returns True if the text is such a relay (so it is not shown as the
        user's prompt)."""
        if text and "teammate-message" in text:
            when = ts or time.time()
            for name in _TEAMMATE_RE.findall(text):
                self.finished_agents[name] = max(when, self.finished_agents.get(name, 0.0))
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
        usage = msg.get("usage") or {}
        if usage:
            self.context_tokens = (
                int(usage.get("input_tokens") or 0)
                + int(usage.get("cache_read_input_tokens") or 0)
                + int(usage.get("cache_creation_input_tokens") or 0)
            )
        content = msg.get("content")
        if not isinstance(content, list):            # a plain-string assistant message
            if isinstance(content, str) and content.strip():
                self.last_text, self.last_text_ts = clean_text(content), ts
                self.last_row_kind = "text"
            return
        for c in content:
            if not isinstance(c, dict):
                continue
            ct = c.get("type")
            if ct == "text":
                txt = c.get("text") or ""
                if txt.strip():
                    self.last_text, self.last_text_ts = clean_text(txt), ts
                    self.last_row_kind = "text"
            elif ct == "tool_use":
                name = c.get("name") or "?"
                inp = c.get("input") or {}
                self.pending_tools[c.get("id")] = (clean_text(name), clean_text(_tool_summary(name, inp)), ts)
                self.last_row_kind = "tool_use"
                cmd = inp.get("command") if isinstance(inp, dict) else None
                if isinstance(cmd, str) and cmd:
                    self.commands.pop(cmd, None)
                    self.commands[cmd] = (clean_text(_tool_summary(name, inp)), ts)
                    if len(self.commands) > COMMANDS_KEPT:
                        del self.commands[next(iter(self.commands))]
                if name in ("Agent", "Task") and isinstance(inp, dict):
                    nm = clean_text(inp.get("name"))
                    if nm:
                        self.spawns.setdefault(nm, {}).update(
                            {"name": nm, "description": clean_text(inp.get("description")), "ts": ts}
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
class Shell:
    """A command Claude Code is running in a shell of its own: a background command, a
    monitor, or a foreground command still in progress."""
    pid: int
    started: float
    command: str                     # as the shell was given it
    label: str = ""                  # the model's description of it, from the transcript
    owner: str = ""                  # agent_id of the subagent that issued it; "" for the head
    owner_name: str = ""             # that subagent's name
    stale: bool = False              # still running, but the agent that started it has stopped


def _shell_command(script: str) -> str:
    """The command inside Claude Code's wrapper, with the shell's quoting of single quotes
    undone; the whole script if the wrapper ever changes shape."""
    m = _EVAL_RE.search(script or "")
    return clean_text(m.group(1).replace("'\"'\"'", "'") if m else script)


@dataclass
class Agent:
    """A subagent / teammate under a head session."""
    agent_id: str
    name: str
    agent_type: str = ""
    description: str = ""
    model: str = ""
    color: str = ""
    status: str = "unknown"          # running | shell | dormant | stale | starting | archived
    archived: bool = False           # from an earlier run of this session: a log, not a live teammate
    transcript: Optional[TranscriptState] = None
    started_ts: Optional[float] = None
    last_activity: Optional[float] = None
    shells: list[Shell] = field(default_factory=list)   # the commands it issued that are still running

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
    """A head Claude Code process: one per terminal tab or desktop-app conversation."""
    pid: int
    session_id: str = ""
    name: str = ""
    cwd: str = ""
    kind: str = ""
    entrypoint: str = ""             # "cli" for a terminal, "claude-desktop" for the Claude app
    started_at: Optional[float] = None
    status: str = "unknown"          # busy | idle | shell | waiting | dead | unknown
    status_detail: str = ""
    status_since: Optional[float] = None
    alive: bool = True
    tty: str = ""
    iterm_session: str = ""
    iterm_title: str = ""
    transcript: Optional[TranscriptState] = None
    agents: list[Agent] = field(default_factory=list)
    git: dict = field(default_factory=dict)      # from git_info(cwd); {} when not a repository
    shells: list[Shell] = field(default_factory=list)   # every shell under the process, subagents' too

    @property
    def own_shells(self) -> list[Shell]:
        return [s for s in self.shells if not s.owner]

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
        self._lock = threading.Lock()           # one `it2 session list` at a time
        self._thread: Optional[threading.Thread] = None

    def start_background(self) -> None:
        """Refresh the tab map on a thread of its own. `it2` can stall for seconds, and
        that must not hold up the once-a-second collector tick, which only reads by_tty."""
        if self._thread or not self.available:
            return
        self._thread = threading.Thread(target=self._loop, name="it2-refresh", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while True:
            self.refresh(force=True)
            time.sleep(self.refresh_s)

    def refresh(self, force: bool = False) -> None:
        if not self.available:
            return
        now = time.time()
        if not force and now - self._last < self.refresh_s:
            return
        if not self._lock.acquire(blocking=False):      # a refresh is already running
            return
        try:
            self._last = now
            self._refresh_locked()
        finally:
            self._lock.release()

    def _refresh_locked(self) -> None:
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
    def activate(bundle_id: str, label: str = "") -> tuple[bool, str]:
        """Bring an application to the front through Launch Services (`open -b`). Unlike an
        AppleScript `activate` this needs no Automation permission, which matters now that
        agent-farm is its own app rather than a TUI inside iTerm2."""
        try:
            r = subprocess.run([OPEN, "-b", bundle_id], capture_output=True, text=True, timeout=5)
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
        # Order matters. First make iTerm2 the active app (through Launch Services: macOS
        # ignores an app activating itself from behind another app), then have iTerm2
        # select the tab and order its window front: an *active* app ordering a window
        # front switches to that window's Space. The other way round raced, and macOS
        # would activate iTerm2 on whatever window it already had on the current Space,
        # leaving the target on a Space the user was not looking at.
        ok, why = self.activate(ITERM_BUNDLE_ID, label)
        if not ok:
            return False, why
        cmd = [IT2, "session", "focus", session_id]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=3)
        except Exception as e:
            log.exception("focus %s: %s failed to run", label, " ".join(cmd))
            return False, repr(e)
        if r.returncode != 0:
            msg = (r.stderr or r.stdout).strip()[:300]
            log.warning("focus %s: rc=%d cmd=%s :: %s", label, r.returncode, " ".join(cmd), msg)
            return False, msg or f"it2 exited {r.returncode}"
        log.info("focus %s: ok (session %s)", label, session_id)
        return True, ""


# --------------------------------------------------------------------------- #
# Collector
# --------------------------------------------------------------------------- #
class Collector:
    def __init__(self):
        self.iterm = ITerm2Map()
        self._transcript_paths: dict[str, Path] = {}
        # Textual's "exclusive" thread workers are only flagged as cancelled, they keep
        # running, so a slow tick would otherwise overlap the next ones over shared state.
        self._busy = threading.Lock()

    def start(self) -> None:
        """Begin background work the app needs: the iTerm2 tab map on its own thread."""
        self.iterm.start_background()

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
                [PS, "-o", "pid=,tty=", "-p", ",".join(str(p) for p in pids)],
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
                    name=clean_text(meta.get("name") or meta.get("agentType")) or agent_id,   # unnamed agents show their type
                    agent_type=clean_text(meta.get("agentType")),
                    description=clean_text(meta.get("description")),
                    model=clean_text(meta.get("model")),
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
                elif inst.transcript and tail.mtime <= inst.transcript.finished_agents.get(a.name, -1.0) + 1.0:
                    a.status = "dormant"         # reported back and silent since; reusable by the head
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
                        name=clean_text(m.get("name")) or "?",
                        agent_type=clean_text(m.get("agentType")),
                        description=clean_text(f"teammate ({m.get('backendType', '')})"),
                        status="running" if inst.alive else "archived",
                        archived=not inst.alive,
                        started_ts=(m.get("joinedAt") or 0) / 1000 or None,
                    ))
            except Exception:
                pass
        agents.sort(key=lambda a: a.started_ts or 0)
        return agents

    # ---- main entry -------------------------------------------------------
    def snapshot(self) -> Optional[list[Instance]]:
        """One pass over the registry, transcripts and processes. Returns None when the
        previous pass is still running, so callers simply skip that tick."""
        if not self._busy.acquire(blocking=False):
            return None
        try:
            return self._snapshot_locked()
        finally:
            self._busy.release()

    def _snapshot_locked(self) -> list[Instance]:
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
                name=clean_text(reg.get("name")),
                cwd=reg.get("cwd") or "",
                kind=reg.get("kind") or "",
                entrypoint=reg.get("entrypoint") or "",
                started_at=(reg.get("startedAt") or 0) / 1000 or None,
                status=reg.get("status") or "unknown",
                status_since=(reg.get("statusUpdatedAt") or 0) / 1000 or None,
            )
            inst.alive = self._pid_alive(pid)
            if not inst.alive:
                inst.status = "dead"
            instances[pid] = inst

        # Claude processes not in the registry (older builds, odd entrypoints).
        procs = self._process_table()
        for inst in self._unregistered(set(instances), procs):
            instances[inst.pid] = inst

        live = [i.pid for i in instances.values() if i.alive]
        ttys = self._ttys_for(live)
        shells = self._shells_under(procs, set(live))
        for inst in instances.values():
            try:
                self._fill_instance(inst, ttys, shells.get(inst.pid, []), now)
            except Exception as e:               # one broken instance must not blank the others
                inst.status_detail = "collector error (see the log)"
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

    @staticmethod
    def _process_table() -> list:
        """Every process with its pid, parent and name, read once per tick for the two
        lookups below. A failure costs only what they add, never the registered sessions."""
        if psutil is None:
            return []
        try:
            return list(psutil.process_iter(["pid", "ppid", "name"]))
        except Exception as e:
            if _debounce.ok("psutil-scan"):
                log.warning("process scan failed: %r", e)
            return []

    @staticmethod
    def _unregistered(known: set[int], procs: Optional[list] = None) -> list[Instance]:
        """`claude` processes with no registry entry. Only the name is read for every
        process; command line and cwd are read per claude process, each under its own
        guard, because macOS refuses the command line of some system processes with a
        plain PermissionError that psutil does not translate."""
        if procs is None:
            procs = Collector._process_table()
        found: list[Instance] = []
        try:
            for p in procs:
                if p.info["name"] != "claude" or p.info["pid"] in known:
                    continue
                try:
                    cmd = p.cmdline() or []
                    inst = Instance(pid=p.info["pid"], kind="unregistered",
                                    started_at=p.create_time(), cwd=p.cwd() or "")
                    if "-n" in cmd and cmd.index("-n") + 1 < len(cmd):
                        inst.name = cmd[cmd.index("-n") + 1]
                    found.append(inst)
                except Exception:                # gone, unreadable, odd argv: skip this one
                    continue
        except Exception as e:
            if _debounce.ok("psutil-scan"):
                log.warning("process scan failed: %r", e)
        return found

    @staticmethod
    def _shells_under(procs: list, heads: set[int]) -> dict[int, list[Shell]]:
        """The command shells each head process is running, by its pid. Only children of a
        live head are looked at; only their argv and start time are read, never logged."""
        found: dict[int, list[Shell]] = {}
        for p in procs:
            info = getattr(p, "info", None) or {}
            parent = info.get("ppid")
            if parent not in heads:
                continue
            try:
                argv = p.cmdline() or []
                if not any(SHELL_MARK in a for a in argv):
                    continue
                shell = Shell(pid=info["pid"], started=p.create_time(), command=_shell_command(argv[-1]))
            except Exception:                    # gone or unreadable: it will show next tick
                continue
            found.setdefault(parent, []).append(shell)
        return found

    @staticmethod
    def _attribute_shells(inst: Instance, shells: list[Shell]) -> None:
        """Name each shell from the transcript that issued its command and give it to that
        agent: the head, or the subagent whose command it is (the latest issue before the
        shell started, when several ran the same command). An agent with nothing else going
        on while one of its shells runs is waiting on it: the 'shell' state. A shell whose
        subagent has stopped mid-task (stale) will never be read: it is stale too."""
        sources = [("", inst.transcript)] + [(a.agent_id, a.transcript) for a in inst.agents if not a.archived]
        for s in shells:
            best = None
            for owner, tr in sources:
                hit = tr.commands.get(s.command) if tr else None
                ts = (hit[1] or 0.0) if hit else 0.0
                if hit and ts <= s.started + SHELL_ISSUE_SLACK_S and (best is None or ts > best[2]):
                    best = (owner, hit[0], ts)
            if best:
                s.owner, s.label = best[0], best[1]
        inst.shells = sorted(shells, key=lambda s: s.started)
        for s in inst.shells:
            s.owner_name = inst.name
        for a in inst.agents:
            a.shells = [s for s in inst.shells if s.owner == a.agent_id]
            for s in a.shells:
                s.owner_name, s.stale = a.name, a.status == "stale"
            if a.shells and a.status == "dormant":
                a.status = "shell"
        if inst.status == "idle" and inst.own_shells:
            inst.status, inst.status_detail = "shell", "waiting on a command it started"

    def _fill_instance(self, inst: Instance, ttys: dict[int, str], shells: list[Shell], now: float) -> None:
        if inst.alive:
            inst.tty = ttys.get(inst.pid, "")
            it = self.iterm.lookup(inst.tty)
            inst.iterm_session = it.get("id", "")
            inst.iterm_title = clean_text(it.get("title") or it.get("name"))
        tpath = self._find_transcript(inst.session_id, inst.cwd) if inst.session_id else None
        if tpath:
            inst.transcript = tail_for(tpath)
            if not inst.name and inst.transcript.title:
                inst.name = inst.transcript.title
            if not inst.cwd:
                inst.cwd = inst.transcript.cwd
        if inst.alive and inst.cwd:
            inst.git = git_info(inst.cwd)
        self._refine_status(inst)
        inst.agents = self._agents_for(inst, tpath)
        self._attribute_shells(inst, shells)

    @staticmethod
    def _refine_status(inst: Instance) -> None:
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
    "busy": "●", "idle": "○", "shell": "○", "waiting": "◐", "dead": "✕", "unknown": "?",
    "running": "●", "dormant": "✓", "archived": "▫", "starting": "◌", "stale": "◍",
}


if __name__ == "__main__":
    c = Collector()
    c.iterm.refresh(force=True)      # the app refreshes this on a thread; the dump is one-shot
    for inst in c.snapshot():
        print(f"{STATUS_GLYPH.get(inst.status, '?')} [{inst.pid}] {inst.name or '(unnamed)'}  "
              f"{inst.project}{'@' + inst.branch if inst.branch else ''}  {inst.status} {fmt_age(inst.status_since)}  "
              f"ctx={fmt_tokens(inst.transcript.context_tokens) if inst.transcript else '?'}  "
              f"tty={inst.tty} iterm={inst.iterm_title!r}")
        print(f"    prompt : {inst.last_prompt}")
        print(f"    doing  : {inst.activity}")
        for a in inst.agents:
            print(f"    └ {STATUS_GLYPH.get(a.status, '?')} {a.name} [{a.agent_type}] {a.status}: {a.activity}")
