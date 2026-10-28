"""
agent-farm — a Textual TUI that watches every Claude Code instance on this Mac.

Head agents are the tree rows and their subagents / teammates nest beneath them, with the
shells each agent is running beneath it. The left column holds the tree, the detail pane for
the selected row, and the expiring pane (the sessions Claude Code deletes next); the usage
column (tokens per day across every transcript, and agents at work) runs down the right.
Keys: ↑/↓ move · enter/space fold · f bring the session forward (iTerm2 tab or Claude app) ·
tab to the expiring pane and k to keep a session · d detail · u usage · m metric ·
s settings · r refresh · q quit
Instances are ordered by the Ordering chosen in Settings (see ordering.py); `--order NAME` overrides for one run.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import shlex
import signal
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from itertools import groupby

from rich.style import Style
from rich.text import Text
from textual import events, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.css.query import NoMatches
from textual.message import Message
from textual.screen import Screen
from textual.widget import Widget
from textual.widgets import (Footer, Header, Label, RadioButton, RadioSet, Static, Switch,
                             Tree)
from textual.widgets.tree import TreeNode

from collector import (GIT_INSTALL_HINT, Agent, Collector, ErrorCounter, Instance, Shell, fmt_age,
                       fmt_tokens, setup_logging)
from apppaths import app_version
from expiring import DAY, DEFAULT_PERIOD_DAYS, Expiring, ExpiryScanner
from expiring import WINDOW_DAYS as EXPIRY_DAYS
from limits import LimitsReport, PlanLimits
from ordering import DEFAULT_ORDERING, ORDERINGS, Ordering, get_ordering
from settings import Settings
from usage import WINDOW_DAYS, UsageScanner, fmt_count

log = logging.getLogger("agent_farm.app")
VERSION = app_version()       # from the Mac app's environment, or the VERSION file in a checkout
errors = ErrorCounter()

REFRESH_S = 1.0
USAGE_SCAN_S = 10.0
LIMITS_POLL_S = 300.0   # plan limits: at most one fetch per this many seconds, and only after work
_monotonic = time.monotonic   # the plan-limit gate's clock; tests replace this, not time.monotonic
LIMITS_REDRAW_S = 60.0  # while idle nothing else redraws the panel; keep its age and "paused" current
SHORT_ROWS = 22        # below this many rows the detail pane hides itself
ERROR_WINDOW_S = 600   # the fleet line's red badge counts warnings this recent
USAGE_PANEL_WIDTH = 50 # must match the #usage width in CSS
# Terminal title: the Mac app shows it in its title bar, iTerm2 in its tab. The in-app
# header keeps the plain TITLE because terminal fonts render emoji unevenly. After the
# name, one chicken per live session that is not idle (busy or waiting).
WINDOW_TITLE = "🌾 agent-farm"
CHICKEN = "🐓"
GUIDE_DEPTH = 3        # tree guide indent per level; must match tree.guide_depth
USAGE_DAYS = WINDOW_DAYS   # the scanner never reads files older than this
USAGE_METRICS = [       # (attribute on DayUsage, label, column header)
    ("out_tokens", "output tokens", "out"),
    ("in_tokens", "input tokens incl. cache", "in"),
    ("messages", "API messages", "msgs"),
]
BAR_EIGHTHS = " ▏▎▍▌▋▊▉█"
# One hue for one measure: the app's "busy" green, dimmed; today is the full step.
BAR_HUE, BAR_HUE_TODAY = "dim #00d75f", "#00d75f"
# Model split: validated dark-surface categorical slots (dataviz skill palette.md), fixed
# order, one glyph texture per slot so the stacked bar reads without color.
MODEL_SLOTS = (("#3987e5", "█"), ("#d95926", "▓"), ("#199e70", "▒"), ("#c98500", "░"))
MODEL_OTHER = ("#888888", "·")
_model_slot: dict[str, int] = {}          # model -> slot index, assigned on first sight


def model_slot(model: str) -> tuple[str, str]:
    if model not in _model_slot:
        _model_slot[model] = len(_model_slot)
    idx = _model_slot[model]
    return MODEL_SLOTS[idx] if idx < len(MODEL_SLOTS) else MODEL_OTHER


def model_split_text(split: list[tuple[str, int]], width: int) -> Text:
    """A full-width stacked bar of output tokens by model, top three plus Other, with a
    legend line of swatch, name and share."""
    t = Text()
    total = sum(n for _, n in split) or 1
    top = split[:3]
    other = sum(n for _, n in split[3:])
    parts = [(m, n, model_slot(m)) for m, n in top]
    if other:
        parts.append(("other", other, MODEL_OTHER))
    # integer cell widths that sum exactly to `width`
    cells = [int(width * n / total) for _, n, _ in parts]
    for k in range(width - sum(cells)):
        cells[k % len(cells)] += 1
    for (m, n, (hue, glyph)), w in zip(parts, cells):
        t.append(glyph * w, style=hue)
    t.append("\n")
    line_len = 0
    for m, n, (hue, glyph) in parts:
        pct = 100 * n / total
        item = f" {m} {'<1' if 0 < pct < 0.5 else f'{pct:.0f}'}%"
        if line_len and line_len + 1 + len(item) + 2 > width:       # wrap the legend
            t.append("\n")
            line_len = 0
        elif line_len:
            t.append("  ")
            line_len += 2
        t.append(glyph, style=hue)
        t.append(item)
        line_len += 1 + len(item)
    t.append("\n")
    return t

# Row colors. A row's icon says its state, and its name whether it is yours to talk to:
# white for a session you can give input to (waiting for you, or idle), otherwise the
# state's own color: green at work, blue at rest, gray stopped, dark gray archived, red
# unreadable. The summary is colored only when it needs you (yellow) or is over.
GREEN, BLUE, YELLOW, WHITE = "#00d75f", "#5f87ff", "#ffd75f", "#eeeeee"
GRAY, DARK_GRAY, RED = "#8a8a8a", "#626262", "#ff5f5f"


@dataclass(frozen=True)
class Look:
    icon: str                         # the triangle or circle, the fleet line's square, the tally mark
    name: str                         # the name, in bold
    summary: str = ""                 # the activity text, in italics; "" keeps the default ink


SESSION_LOOK = {
    "waiting": Look(YELLOW, WHITE, YELLOW),     # needs you: permission prompt, question, plan approval
    "busy": Look(GREEN, GREEN),                 # working on your input
    "idle": Look(BLUE, WHITE),                  # finished, ready for your next instruction
    "shell": Look(BLUE, WHITE),                 # idle while a command it started runs
    "dead": Look(GRAY, GRAY, GRAY),             # inactive: a registry entry left behind
    "unknown": Look(GRAY, GRAY, GRAY),          # inactive: a claude process with no registry entry
    "error": Look(RED, RED, RED),               # agent-farm could not read it (see the log)
}
AGENT_LOOK = {
    "running": Look(GREEN, GREEN),
    "starting": Look(GREEN, GREEN),             # spawned, no transcript yet
    "shell": Look(BLUE, BLUE),                  # finished its turn, waiting on a command it started
    "dormant": Look(BLUE, BLUE),                # finished, resident, can be messaged again
    "stale": Look(GRAY, GRAY, GRAY),            # stopped mid-task and silent: likely killed
    "archived": Look(DARK_GRAY, DARK_GRAY, DARK_GRAY),   # from an earlier run: a log only
}
SHELL_RUNNING, SHELL_STALE = Look(GREEN, GREEN), Look(GRAY, GRAY, GRAY)
STATUS_WORD = {"shell": "idle"}               # the detail pane's word for a state, where it differs
# The plan-limit bars warn on a scale of their own, not in the row colors.
LIMIT_WARN, LIMIT_OVER = "bold #ff9500", RED


def look_of(obj) -> Look | None:
    """How a row is drawn: by its kind (session, subagent, shell, archive) and state."""
    if isinstance(obj, Instance):
        if obj.status_detail.startswith("collector error"):
            return SESSION_LOOK["error"]
        return SESSION_LOOK.get(obj.status, SESSION_LOOK["unknown"])
    if isinstance(obj, Agent):
        return AGENT_LOOK.get(obj.status, AGENT_LOOK["dormant"])
    if isinstance(obj, Shell):
        return SHELL_STALE if obj.stale else SHELL_RUNNING
    if isinstance(obj, ArchiveGroup):
        return AGENT_LOOK["archived"]
    return None


def _styled(base: str, hue: str) -> str:
    return f"{base} {hue}" if hue else base


TOGGLE_META = Style.from_meta({"toggle": True})     # lets a mouse click on the glyph fold the row


def is_shell_key(key) -> bool:
    return isinstance(key, str) and key.rsplit("/", 1)[-1].startswith("s:")


class AgentTree(Tree):
    """One icon per row, in the row's state color. Agents get triangles, filled ▶ / ▼ when
    the row has children to fold and hollow ▷ when it has none; a shell, which never has
    children, gets a half-filled circle.
    Only the triangle folds a row under the mouse: a click on the name selects it and leaves
    it as it was. From the keyboard, enter and space fold."""
    ICON_NODE = "▶ "
    ICON_NODE_EXPANDED = "▼ "
    ICON_LEAF = "▷ "
    ICON_SHELL = "◐ "
    BINDINGS = [Binding("enter", "toggle_node", "Toggle", show=False)]

    def __init__(self, *args, look_lookup=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.look_lookup = look_lookup or (lambda key: None)
        self.auto_expand = False           # selecting (a click on the name) never folds

    def render_label(self, node: TreeNode, base_style: Style, style: Style) -> Text:
        label = node._label.copy()
        # The highlight's background and weight, never its text color: the selected row
        # keeps the colors that say its state.
        label.stylize(style.without_color + Style(bgcolor=style.bgcolor))
        if is_shell_key(node.data):
            icon, icon_style = self.ICON_SHELL, base_style
        elif node.allow_expand:
            icon = self.ICON_NODE_EXPANDED if node.is_expanded else self.ICON_NODE
            icon_style = base_style + TOGGLE_META
        else:
            icon = self.ICON_LEAF
            icon_style = base_style
        look = self.look_lookup(node.data) if node.data is not None else None
        if look:
            icon_style = icon_style + Style.parse(look.icon)
        return Text.assemble((icon, icon_style), label)


STRIP_MAX = 16                       # cap on the agent strip column (16 asterisks = 80 agents)
STRIP_ORDER = ("running", "starting", "shell", "dormant", "stale")
STRIP_AT_WORK, STRIP_AT_REST = "▸", "▹"
STRIP_FIVE = "✱"                      # one asterisk tallies five agents of that state


def agent_glyphs(agents: list[Agent]) -> list[tuple[str, str]]:
    """One small triangle per current-run subagent, one asterisk per five, in its icon color
    and ordered running → starting → shell → dormant → stale. Filled means at work and
    hollow means not, so it reads without color too."""
    counts = {st: 0 for st in STRIP_ORDER}
    for a in agents:
        if not a.archived and a.status in counts:
            counts[a.status] += 1
    glyphs: list[tuple[str, str]] = []
    for st in STRIP_ORDER:
        n = counts[st]
        style = AGENT_LOOK[st].icon
        mark = STRIP_AT_WORK if st in ("running", "starting") else STRIP_AT_REST
        glyphs += [(STRIP_FIVE, style)] * (n // 5)
        glyphs += [(mark, style)] * (n % 5)
    if len(glyphs) > STRIP_MAX:
        glyphs = glyphs[:STRIP_MAX - 1] + [("+", "dim")]
    return glyphs


APP_MARK = ("⧉", "cyan")              # the session runs inside the Claude desktop app


def row_glyphs(i: Instance) -> list[tuple[str, str]]:
    """What a head row shows in the strip column: the desktop-app marker, then its agents.
    Keeping the marker in that column lines it up with the tallies on every row instead of
    trailing the name."""
    return ([APP_MARK] if i.is_app else []) + agent_glyphs(i.agents)


def strip_column_width(instances: list[Instance]) -> int:
    """The strip column is exactly as wide as the widest strip on screen right now."""
    return max((len(row_glyphs(i)) for i in instances), default=0)


def agent_strip(glyphs: list[tuple[str, str]], width: int) -> Text:
    t = Text()
    for g, st in glyphs:
        t.append(g, style=st)
    t.pad_right(width - len(glyphs))
    return t


HEAD_GLYPH = "■"                      # one square per head session in the fleet line
HEAD_ORDER = ("waiting", "busy", "idle", "dead", "unknown")


def _fleet_group(i: Instance) -> str:
    return "idle" if i.status == "shell" else i.status      # drawn as idle, its shells beneath it


def fleet_line(instances: list[Instance], show_text: bool,
               error_count: int, it2_ok: bool) -> Text:
    """The one-line summary above the tree: a square per head session grouped and colored
    by state (waiting → busy → idle → inactive), the fleet-wide subagent tally in the same
    triangles-and-asterisks system as the rows, then red notes only when it2 is missing or
    warnings were logged recently.
    With the status-text setting on, the counts are also spelled out."""
    live = [i for i in instances if i.alive]
    t = Text()
    for st in HEAD_ORDER:
        n = sum(1 for i in instances if (_fleet_group(i) == st and (i.alive or st in ("dead", "unknown"))))
        if n:
            t.append(HEAD_GLYPH * n, style=SESSION_LOOK[st].icon)
    if not instances:
        t.append("no Claude instances", style="dim")
    all_agents = [a for i in live for a in i.agents]
    glyphs = agent_glyphs(all_agents)
    if glyphs:
        t.append("  ")
        for g, st in glyphs:
            t.append(g, style=st)
    if show_text:
        busy = sum(1 for i in live if i.status == "busy")
        waiting = sum(1 for i in live if i.status == "waiting")
        shell = sum(1 for i in live if i.status == "shell")
        running = sum(1 for a in all_agents if a.status == "running")
        t.append("  ")
        t.append(f"{len(live)} sessions · {busy} busy · {waiting} waiting · "
                 + (f"{shell} waiting on a shell · " if shell else "") + f"{running} agents running")
    if not it2_ok:
        t.append("  ")
        t.append("it2 missing — focus (f) unavailable", style=RED)
    if error_count:                      # recent problems only; the tooltip has the total
        t.append("  ")
        t.append(f"⚠ {error_count} (see the log)", style=RED)
    return t


def fleet_tooltip(instances: list[Instance], ordering: Ordering, error_count: int, it2_ok: bool) -> str:
    """Everything the fleet line encodes, plus the active ordering, as words (on hover)."""
    live = [i for i in instances if i.alive]
    counts = {st: sum(1 for i in live if i.status == st) for st in ("waiting", "busy", "shell", "idle")}
    inactive = len(instances) - len(live) + sum(1 for i in live if i.status == "unknown")
    agents = [a for i in live for a in i.agents if not a.archived]
    ag = {st: sum(1 for a in agents if a.status == st) for st in STRIP_ORDER}
    archived = sum(1 for i in instances for a in i.agents if a.archived)
    shells = [s for i in live for s in i.shells]
    lines = [
        f"{len(instances)} Claude session{'s' if len(instances) != 1 else ''}: "
        f"{counts['waiting']} waiting for you, {counts['busy']} busy, {counts['idle'] + counts['shell']} idle"
        + (f" ({counts['shell']} waiting on a shell)" if counts["shell"] else "") + f", {inactive} inactive",
        "  one ■ per session, colored yellow / green / blue / gray in that order",
        f"{len(agents)} current-run subagent{'s' if len(agents) != 1 else ''}: "
        f"{ag['running']} running, {ag['starting']} starting, {ag['shell']} waiting on a shell, "
        f"{ag['dormant']} dormant, {ag['stale']} stale"
        + (f"  (+{archived} archived, not shown)" if archived else ""),
        "  ▸ at work  ▹ at rest (blue) or stopped (gray) · ✱ = five of a kind",
    ]
    if shells:
        stale = sum(1 for s in shells if s.stale)
        lines.append(f"{len(shells)} shell{'s' if len(shells) != 1 else ''} running"
                     + (f" ({stale} whose agent has stopped)" if stale else ""))
    lines.append(f"ordering: {ordering.name} — {ordering.description}")
    if not it2_ok:
        lines.append("iTerm2's it2 CLI was not found, so the f key cannot focus tabs")
    if error_count:
        lines.append(f"{error_count} warning{'s' if error_count != 1 else ''} in the last {ERROR_WINDOW_S // 60} minutes "
                     f"({errors.count} since launch) — details in the log (Monitor ▸ Open Log Folder)")
    return "\n".join(lines)


def _fit_name(name: str, width: int) -> str:
    if width <= 0:
        return name
    if len(name) > width:
        return name[: width - 1] + "…"
    return name.ljust(width)


def _status_words(status: str, since: float | None, now: float, look: Look | None) -> Text:
    t = Text("  ")
    t.append(STATUS_WORD.get(status, status), style=look.icon if look else "")
    if since:
        t.append(" ")
        t.append(fmt_age(since, now), style="dim")
    return t


def instance_label(i: Instance, now: float, show_status: bool = False, name_width: int = 0,
                   strip_width: int = 0) -> Text:
    """Row = name · strip (app marker, agents) · activity. The name comes first so subagent
    rows, indented by the tree guides, visibly hang under their lead's name; the strip sits
    in its own aligned column right after the name field."""
    look = look_of(i)
    t = Text()
    name = i.name or f"pid {i.pid}"
    t.append(_fit_name(name, name_width), style=_styled("bold", look.name))
    glyphs = row_glyphs(i)
    if strip_width or glyphs:
        t.append(" ")
        t.append_text(agent_strip(glyphs, strip_width))
    if show_status:
        t.append_text(_status_words(i.status, i.status_since, now, look))
    act = i.activity
    if act:
        t.append("  ")
        t.append(act, style=_styled("italic", look.summary))
    return t


# Claude Code's own colors for named agents (its default theme). Rows color names by state,
# so a teammate's color is shown in the detail pane only. The meta file holds just the
# name, and passed on as is, pink and orange would not parse and the rest are basic ANSI
# colors that Textual's dark theme repaints (its blue is a violet).
AGENT_COLORS = {"red": "#dc2626", "blue": "#6a9bcc", "green": "#16a34a", "yellow": "#ca8a04",
                "purple": "#827dbd", "orange": "#d97757", "pink": "#c46686", "cyan": "#0891b2"}


def agent_label(a: Agent, now: float, show_status: bool = False, name_width: int = 0) -> Text:
    look = look_of(a)
    t = Text()
    name = a.name + (f" ({a.agent_type})" if a.agent_type and a.agent_type != a.name else "")
    t.append(_fit_name(name, name_width), style=_styled("bold", look.name))
    if show_status:
        t.append_text(_status_words(a.status, a.last_ts, now, look))
    act = a.activity
    if act:
        t.append("  ")
        t.append(act, style=_styled("italic", look.summary))
    return t


SHELL_ROW_AFTER_S = 5.0   # a command gets a row once it has run this long, so quick ones never flicker


def shell_rows(shells: list[Shell], now: float) -> list[Shell]:
    return [s for s in shells if now - s.started >= SHELL_ROW_AFTER_S]


def shell_command_line(s: Shell) -> str:
    return s.command.strip().split("\n", 1)[0]


def shell_label(s: Shell, now: float, name_width: int = 0) -> Text:
    """Shell row = the command in the name column · how long it has run · what it is for."""
    look = look_of(s)
    t = Text()
    t.append(_fit_name(shell_command_line(s), name_width), style=_styled("bold", look.name))
    t.append("  ")
    t.append(fmt_age(s.started, now) + (f" · {s.label}" if s.label else ""), style=_styled("italic", look.summary))
    return t


class ArchiveGroup:
    """The accordion node holding a head agent's archived subagents."""
    def __init__(self, inst: Instance, agents: list[Agent]):
        self.inst, self.agents = inst, agents


_RELAY_RE = re.compile(r'<(cross-session-message|teammate-message|task-notification)\b([^>]*)>')


def _queued_summary(content: str) -> str:
    """Queued items are usually your typing, but Claude Code also routes relays through
    the same queue. Name those instead of showing their raw markup."""
    m = _RELAY_RE.match(content.strip())
    if not m:
        return content[:200]
    kind, attrs = m.group(1), m.group(2)
    who = re.search(r'(?:teammate_id|from)="([^"]+)"', attrs)
    src = who.group(1) if who else ""
    if kind == "task-notification":
        return "background task finished (result pending delivery)"
    if kind == "teammate-message":
        return f"message from teammate {src}" if src else "message from a teammate"
    return f"message from another session ({src})" if src else "message from another session"


def _shell_lines(out: Text, shells: list[Shell], now: float) -> None:
    """The detail pane's list of running shells, quick ones included: how long each has run,
    what it is for and, when the transcript named it, the command itself."""
    if not shells:
        return
    out.append("shells  (commands still running)\n", style="bold dim")
    for s in shells:
        command = shell_command_line(s)[:160]
        out.append(f"{fmt_age(s.started, now):>7}  ", style="dim")
        out.append(s.label or command, style=look_of(s).name)
        if s.owner:
            out.append(f"  ({s.owner_name or s.owner})", style="dim")
        out.append("\n")
        if s.label:
            out.append(f"         $ {command}\n", style="dim")
    out.append("\n")


def detail_text(obj, now: float) -> Text:
    out = Text()

    def kv(k: str, v, style: str = ""):
        if v in (None, "", 0, 0.0):
            return
        out.append(f"{k:<10}", style="bold dim")
        out.append(f"{v}\n", style=style)

    if isinstance(obj, Instance):
        i = obj
        out.append(i.name or f"pid {i.pid}", style="bold underline")
        out.append("\n")
        kv("status", f"{STATUS_WORD.get(i.status, i.status)} for {fmt_age(i.status_since, now)}"
           + (f"  ({i.status_detail})" if i.status_detail else ""),
           look_of(i).icon)
        if i.is_app:
            kv("where", "Claude desktop app  (f brings the app forward)", "cyan")
        kv("cwd", i.cwd or "(unknown)")
        if i.git and i.git.get("error") == "no git":
            kv("git", f"git not found: {GIT_INSTALL_HINT}", "yellow")
        elif i.git and i.git.get("error"):
            kv("git", "folder not readable yet: allow agent-farm under System Settings > "
                      "Privacy & Security > Files and Folders", "yellow")
        elif i.git:
            g = i.git
            line = g.get("repo", "")
            if i.branch:
                line += f" @ {i.branch}"
            if g.get("worktree"):
                kind = "Claude worktree" if g.get("claude_worktree") else "worktree"
                line += f"   · {kind} of {g.get('main_repo') or 'main checkout'}"
            if g.get("remote"):
                line += f"   ← {g['remote']}"
            if g.get("root") and g["root"] != i.cwd:
                line += f"   (repo root {g['root']})"
            kv("git", line, "cyan")
        else:
            kv("git", "not a git repository", "dim")
        effort = i.transcript.effort if i.transcript else ""
        kv("model", i.model + (f" · {effort} effort" if effort else ""))
        kv("started", fmt_age(i.started_at, now) + " ago")
        if i.transcript:
            tr = i.transcript
            kv("context", f"{fmt_tokens(tr.context_tokens)} tokens"
               + (f"   cost ${tr.cost_usd:.2f}" if tr.cost_usd else ""))
            out.append("\n")
            if tr.away_summary:
                out.append("summary\n", style="bold dim")
                out.append(tr.away_summary.strip() + "\n\n")
            if tr.last_prompt:
                out.append(f"last prompt  {fmt_age(tr.last_prompt_ts, now)} ago\n", style="bold dim")
                out.append(tr.last_prompt.strip()[:800] + "\n\n", style="cyan")
            if tr.current_tool:
                name, summ, ts = tr.current_tool
                out.append(f"running tool  {fmt_age(ts, now)}\n", style="bold dim")
                out.append(f"{name}: {summ}\n\n", style="green")
            _shell_lines(out, i.shells, now)
            # The queue lives in the process's memory, so anything queued before this
            # process started cannot still be waiting.
            queued = [q for q in tr.queued
                      if q.get("content") and (not i.started_at or (q.get("ts") or 0) >= i.started_at)]
            if queued:
                out.append("queued  (input waiting for this session's next turn)\n", style="bold dim")
                for q in queued:
                    out.append("• ")
                    out.append(_queued_summary(q["content"]) + "\n", style="yellow")
                out.append("\n")
            if tr.last_text:
                out.append(f"last message  {fmt_age(tr.last_text_ts, now)} ago\n", style="bold dim")
                out.append(tr.last_text.strip()[:1500] + "\n")
        else:
            out.append("\n(no transcript found yet)\n", style="dim")
    elif isinstance(obj, ArchiveGroup):
        g = obj
        out.append(f"archived agents of {g.inst.name or g.inst.pid}", style="bold underline")
        out.append("\n")
        kv("count", len(g.agents))
        times = [a.last_activity for a in g.agents if a.last_activity]
        if times:
            kv("active", f"{time.strftime('%b %d %H:%M', time.localtime(min(times)))} → "
                         f"{time.strftime('%b %d %H:%M', time.localtime(max(times)))}")
        kv("this run", "started " + fmt_age(g.inst.started_at, now) + " ago")
        stopped = sum(1 for a in g.agents if a.stopped)
        if stopped < len(g.agents):
            out.append("\nThese ran in an earlier process of this session. Teammates live inside the\n"
                       "head process, so they were not restarted on resume; only their transcripts\n"
                       "remain.", style="dim")
        if stopped:
            out.append(f"\n{stopped} stopped mid-task in this run and have been silent over a day,\n"
                       "so they are not coming back.", style="dim")
        out.append(" Expand the row to browse them.\n", style="dim")
    elif isinstance(obj, Agent):
        a = obj
        out.append(a.name, style="bold underline")
        out.append("\n")
        kv("status", f"{a.status} for {fmt_age(a.last_ts, now)}"
           + ("  (stopped mid-task over a day ago; log only)" if a.stopped
              else "  (log only, from an earlier run)" if a.archived else "")
           + ("  (waiting on a command it started)" if a.status == "shell" else ""),
           look_of(a).icon)
        kv("type", a.agent_type)
        if a.color in AGENT_COLORS:
            kv("color", f"{a.color}  (Claude Code's color for this teammate)", AGENT_COLORS[a.color])
        kv("task", a.description)
        kv("model", a.model)
        kv("started", fmt_age(a.started_ts, now) + " ago")
        if a.transcript:
            tr = a.transcript
            kv("context", f"{fmt_tokens(tr.context_tokens)} tokens")
            kv("last event", fmt_age(tr.last_ts, now) + " ago")
            out.append("\n")
            if tr.current_tool:
                name, summ, ts = tr.current_tool
                out.append(f"running tool  {fmt_age(ts, now)}\n", style="bold dim")
                out.append(f"{name}: {summ}\n\n", style="green")
            _shell_lines(out, a.shells, now)
            if tr.last_text:
                out.append("last message\n", style="bold dim")
                out.append(tr.last_text.strip()[:1500] + "\n")
    elif isinstance(obj, Shell):
        s = obj
        out.append(shell_command_line(s), style="bold underline")
        out.append("\n")
        kv("status", ("stale" if s.stale else "running") + f" for {fmt_age(s.started, now)}"
           + ("  (the agent that started it has stopped, so nothing will read its result)" if s.stale else ""),
           look_of(s).icon)
        kv("for", s.label)
        kv("agent", s.owner_name)
        out.append("\ncommand\n", style="bold dim")
        out.append(s.command.strip()[:1500] + "\n")
    return out


def title_sequence(title: str) -> str:
    """OSC 0 sets the terminal's window title; Textual never sets one itself. An empty
    title hands the choice back to the terminal."""
    return f"\x1b]0;{title}\x07"


def fleet_working(instances: list[Instance]) -> bool:
    """Is anything spending tokens: a live session that is busy, or a live subagent or
    teammate that is running or starting (even while its lead sits idle)? Waiting for input
    and idle do not count."""
    return any(i.alive and (i.status == "busy" or
                            any(not a.archived and a.status in ("running", "starting") for a in i.agents))
               for i in instances)


def window_title(instances: list[Instance]) -> str:
    n = sum(1 for i in instances if i.alive and i.status not in ("idle", "shell"))
    return f"{WINDOW_TITLE} {CHICKEN * n}" if n else WINDOW_TITLE


def _bar(frac: float, width: int) -> str:
    cells = max(0.0, min(1.0, frac)) * width
    full = int(cells)
    s = "█" * full
    if full < width:
        eighth = int(round((cells - full) * 8))
        if eighth:
            s += BAR_EIGHTHS[eighth]
    return s.ljust(width)


PACE_TICK = "┃"
PACE_TICK_ON_TRACK = "bold #ffffff"
PACE_TICK_NOTCH = "#121212"   # the panel background, so over the fill the tick is a gap cut into it


def _limit_bar(pct: float, width: int, elapsed: float | None = None) -> Text:
    """A budget bar: the used share in green, orange from 75% and red from 90%, over a dim
    track so the unused share stays visible (the percent beside it says the same).
    `elapsed` (0..1, how far through its window the clock is) adds a tick where an even
    pace would be: fill that stops short of the tick is under pace, fill past it is ahead.
    Over the fill the tick is a notch, over the track a bright line."""
    hue = LIMIT_OVER if pct >= 90 else LIMIT_WARN if pct >= 75 else BAR_HUE_TODAY
    filled = _bar(pct / 100, width).rstrip()
    cells = [(ch, hue) for ch in filled] + [("·", "dim")] * (width - len(filled))
    if elapsed is not None and width > 0:
        i = min(width - 1, int(elapsed * width))
        whole = int(max(0.0, min(1.0, pct / 100)) * width)          # cells filled edge to edge
        cells[i] = (PACE_TICK, Style(color=PACE_TICK_NOTCH, bgcolor=Style.parse(hue).color)
                    if i < whole else PACE_TICK_ON_TRACK)
    t = Text()
    for style, run in groupby(cells, key=lambda c: c[1]):
        t.append("".join(ch for ch, _ in run), style=style)
    return t


def fmt_countdown(at: datetime | None, now: float) -> str:
    """Time left until a window resets: '4d 5h', '2h54m', '38m'. 'reset' once it has: the
    reading is from before the reset, so its bar is out of date until the next fetch."""
    if at is None:
        return ""
    left = int(at.timestamp() - now)
    if left <= 0:
        return "reset"
    d, rem = divmod(left, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    if d:
        return f"{d}d{h:>2}h"
    if h:
        return f"{h}h{m:02d}m"
    return f"{m}m" if m else "<1m"


def limits_text(rep: LimitsReport | None, enabled: bool, width: int, now: float | None = None,
                polling: bool = True) -> Text:
    """The plan-limit block of the usage panel: what /usage shows in Claude Code, one bar per
    window. Off: a one-line pointer to Settings. Failed: the last bars plus the reason.
    Not polling (nothing has worked since the last fetch): the age keeps growing, so the
    header says it is paused."""
    t = Text()
    if not enabled:
        t.append("plan limits · off  (s: Settings)\n", style="dim")
        return t
    rep = rep or LimitsReport()
    now = now or time.time()
    head = "plan limits"
    if rep.plan:
        head += f" · {rep.plan}"
    if rep.fetched_at:
        head += f" · {fmt_age(rep.fetched_at, now)} ago"
        if not polling:
            head += " · paused"
    elif not rep.error:
        head += " · fetching…"
    t.append(head + "\n", style="dim")
    if rep.error:
        t.append(f"unavailable: {rep.error}\n", style="dim" if rep.windows else RED)
    if not rep.windows:
        return t
    label_w = max(len(w.label) for w in rep.windows)
    reset_w = 6                                         # "13h05m" left until the window resets
    bar_w = max(8, width - label_w - 1 - 1 - 4 - 2 - reset_w)
    for w in rep.windows:
        t.append(f"{w.label:<{label_w}} ", style="dim")
        t.append_text(_limit_bar(w.used_pct, bar_w, w.elapsed(now)))
        t.append(f" {int(w.used_pct):>3}%", style="bold" if w.used_pct >= 75 else "")
        left = fmt_countdown(w.resets_at, now)
        if left:
            t.append(f"  {left:>{reset_w}}", style="dim")
        t.append("\n")
    return t


# The agents-at-work chart: one wheat stalk per clock hour, its height the number of agents
# running in that hour. The stem and the box-drawing sprout are drawn by the terminal itself
# (SwiftTerm draws U+2500-257F), and SF Mono has the grain head.
ACTIVITY_ROWS = 6
FARM_REDRAW_S = 60.0    # the chart moves with the clock, so it redraws on its own too
STALK, SPROUT, GRAIN = "┃", "╻", "Ψ"
STALK_HUE, GRAIN_HUE = "#5faf5f", "#e5b45a"        # green stem, ripe grain


def _activity_unit(peak: float, rows: int) -> float:
    """Agents per row: the finest step that fits the peak, so 1.5 agents is three rows."""
    for u in (0.25, 0.5, 1, 2, 5, 10, 20, 50, 100):
        if peak <= u * rows:
            return u
    return peak / rows


def _hour_label(ts: float) -> str:
    h = datetime.fromtimestamp(ts).hour
    return f"{h % 12 or 12}{'a' if h < 12 else 'p'}"


def activity_text(bins: list[tuple[float, float]], width: int, now: float | None = None) -> Text:
    """Agents at work over the last day: a stalk per hour, oldest at the left and the current
    hour at the right, its height the agents running in that hour (sessions and subagents,
    so three agents running half an hour each make 1.5). An hour with some work that rounds
    to no rows still shows a sprout. The figures to the right say what the stalks show: this
    hour so far, the busiest hour, the average over the whole chart, and the agent-hours."""
    now = time.time() if now is None else now
    t = Text()
    t.append(f"agents at work · last {len(bins)} hours\n", style="dim")
    vals = [v for _, v in bins]
    peak = max(vals, default=0.0)
    if peak <= 0:
        t.append("no agent has run in the last day\n" if bins else "", style="dim")
        return t
    unit = _activity_unit(peak, ACTIVITY_ROWS)
    heights = [round(v / unit) for v in vals]
    whole = [r for r in range(1, ACTIVITY_ROWS + 1) if (r * unit) % 1 == 0]
    step = max(1, -(-len(whole) // 3))                     # at most three labels
    labels = {r: f"{r * unit:g}" for r in whole[::-1][::step]}
    gut = max((len(x) for x in labels.values()), default=1)
    peak_at = bins[vals.index(peak)][0]
    hours_total = sum(v * ((bins[k + 1][0] if k + 1 < len(bins) else now) - a) / 3600
                      for k, (a, v) in enumerate(bins))
    span_h = (now - bins[0][0]) / 3600                     # 23 whole hours and this one so far
    side = [("now", f"{vals[-1]:.1f}"), ("peak", f"{peak:.1f} at {_hour_label(peak_at)}"),
            ("avg", f"{hours_total / span_h:.1f}" if span_h > 0 else "–"),
            ("day", f"{hours_total:.1f} agent-h")]
    side_w = max(len(k) for k, _ in side)
    for r in range(ACTIVITY_ROWS, 0, -1):
        lab = labels.get(r, "")
        t.append(f"{lab:>{gut}}{'┤' if lab else '│'}", style="dim")
        for v, h in zip(vals, heights):
            if h > r:
                t.append(STALK, style=STALK_HUE)
            elif h == r:
                t.append(GRAIN, style=GRAIN_HUE)
            elif r == 1 and h == 0 and v > 0:
                t.append(SPROUT, style=STALK_HUE)
            else:
                t.append(" ")
        i = ACTIVITY_ROWS - r
        if i < len(side) and gut + 1 + len(bins) + 3 + side_w + 1 + len(side[i][1]) <= width:
            k, val = side[i]
            t.append(f"   {k:<{side_w}} ", style="dim")
            t.append(val)
        t.append("\n")
    ticks = [k for k, (a, _) in enumerate(bins) if datetime.fromtimestamp(a).hour % 6 == 0]
    t.append(" " * gut + "└" + "".join("┬" if k in ticks else "─" for k in range(len(bins))) + "\n",
             style="dim")
    under = [" "] * (len(bins) + 4)
    for k, lab in [(k, _hour_label(bins[k][0])) for k in ticks] + [(len(bins) - 2, "now")]:
        if k >= 0 and all(c == " " for c in under[max(0, k - 1):k + len(lab)]):
            under[k:k + len(lab)] = lab
    t.append(" " * (gut + 1) + "".join(under).rstrip() + "\n", style="dim")
    return t


# The expiring pane: the sessions Claude Code deletes next (see expiring.py), soonest first,
# under a chart of how many turn old on each of the next two weeks' days. It sits at the
# bottom of the left column as tall as the agents-at-work chart beside it, so the lines
# above the two meet across the window.
BOTTOM_ROWS = ACTIVITY_ROWS + 5        # chart title, rows, axis, hour labels, blank, border
EXPIRY_SCAN_S = 60.0
EXPIRY_CHART_ROWS = 3
EXPIRY_HEAD_ROWS = 1 + EXPIRY_CHART_ROWS + 1   # title, chart, weekday labels
EXPIRY_NAME_W = 28
EXPIRY_WHEN_W = 14                     # "expires today", "expires Nov 14", "expired"
DAY_BLOCK, DAY_NONE = "██", "▁▁"
KEEP_LABEL = " keep "
CURSOR_BG = "#1f3b5c"                  # the tree's focused cursor bar


def _day_month(ts: float) -> str:
    d = datetime.fromtimestamp(ts)
    return f"{d:%b} {d.day}"


def _expires_when(e: Expiring, now: float) -> str:
    """When the session turns old enough to delete. It is still there until Claude Code's
    next cleanup after that, and keep still saves it: hence expires, never deleted."""
    if e.deletes_at <= now:
        return "expired"
    days = (datetime.fromtimestamp(e.deletes_at).date() - datetime.fromtimestamp(now).date()).days
    if days == 0:
        return "expires today"
    if days < 7:
        return f"expires {datetime.fromtimestamp(e.deletes_at):%a}"
    return f"expires {_day_month(e.deletes_at)}"


def _megabytes(n: int) -> str:
    return f"{n / 1e6:.1f} MB" if n >= 100_000 else f"{max(1, round(n / 1e3))} kB"


def _fit(s: str, width: int) -> str:
    return (s if len(s) <= width else s[:max(0, width - 1)] + "…").ljust(width)


def _last_folders(cwd: str) -> str:
    return "/".join([p for p in cwd.split("/") if p][-2:])


def expiry_room(lines: int, n: int) -> int:
    """How many sessions fit under the chart: all of them, or one fewer to make room for
    the line that says how many more there are."""
    avail = max(0, lines - EXPIRY_HEAD_ROWS)
    return n if n <= avail else max(0, avail - 1)


def expiring_text(entries: list[Expiring], now: float, width: int, lines: int,
                  period: float | None, cursor: int | None = None, top: int = 0) -> Text:
    """The pane's text. Each session row carries its index as `row` meta and its keep button
    as `keep` meta, for the mouse. Yellow is two days or less to go: it needs you."""
    t = Text()
    pending = [e for e in entries if not e.kept_at]
    t.append("expiring", style=f"bold {YELLOW if any(e.soon(now) for e in pending) else GRAY}")
    if period is None:
        t.append("  Claude Code keeps every session here (cleanupPeriodDays is 0)\n", style=f"italic {GRAY}")
        return t
    t.append(f"  Claude Code deletes a session {period:g} days after its last use\n", style=f"italic {GRAY}")
    today = datetime.fromtimestamp(now).date()
    # Each day's bar stacks its sessions in list order, the soonest at the bottom, so the
    # selected session lights its own block (or the count, when it is above the chart).
    counts = [0] * EXPIRY_DAYS
    lit_day, lit_level = -1, 0
    for i, e in enumerate(entries):
        if e.kept_at:
            continue
        d = max(0, (datetime.fromtimestamp(e.deletes_at).date() - today).days)
        if d < EXPIRY_DAYS:
            counts[d] += 1
            if i == cursor:
                lit_day, lit_level = d, counts[d]
    for level in range(EXPIRY_CHART_ROWS, 0, -1):
        t.append("  ")
        for d, n in enumerate(counts):
            hue = YELLOW if d < 2 else GRAY
            if level == EXPIRY_CHART_ROWS and n > EXPIRY_CHART_ROWS:
                lit = d == lit_day and lit_level >= level
                t.append(f"{n:>2}", style=f"bold {WHITE if lit else hue}")   # taller than the chart: how many
            elif n >= level:
                t.append(DAY_BLOCK, style=WHITE if (d, level) == (lit_day, lit_level) else hue)
            elif level == 1:
                t.append(DAY_NONE, style=DARK_GRAY)
            else:
                t.append("  ")
            t.append(" ")
        t.append("\n")
    t.append("  ")
    for d in range(EXPIRY_DAYS):
        label = (today + timedelta(days=d)).strftime("%a")[:2]
        t.append(label, style=f"bold {YELLOW}" if d == 0 else YELLOW if d == 1 else "dim")
        t.append(" ")
    t.append("\n")
    if not entries:
        t.append(f"  nothing turns {period:g} days old in the next two weeks\n", style="dim")
        return t
    room = expiry_room(lines, len(entries))
    top = max(0, min(top, len(entries) - room))
    name_w = min(EXPIRY_NAME_W, max(12, width // 4))
    where_w = max(0, width - (2 + 2 + name_w + 1 + EXPIRY_WHEN_W + 8 + 2 + 1 + len(KEEP_LABEL)))
    for i in range(top, top + room):
        e = entries[i]
        row = Text()
        button_at = None
        if e.kept_at:
            row.append("✓ ", style=GREEN)
            row.append(_fit(e.title or "untitled", name_w), style=f"bold {GREEN}")
            row.append(f" kept · safe until {_day_month(e.kept_at + period * DAY)}", style=GREEN)
        else:
            soon = e.soon(now)
            row.append("▷ ", style=YELLOW if soon else GRAY)
            row.append(_fit(e.title or "untitled", name_w), style=f"bold {WHITE if soon else GRAY}")
            row.append(" ")
            row.append(f"{_expires_when(e, now):<{EXPIRY_WHEN_W}}", style=f"italic {YELLOW if soon else GRAY}")
            row.append(f"{_megabytes(e.size):>8}  ", style=GRAY)
            row.append(_fit(_last_folders(e.cwd), where_w), style=GRAY)
            row.append(" ")
            button_at = len(row)
            row.append(KEEP_LABEL, style=Style(bold=True, color="#111111", bgcolor=YELLOW if soon else GRAY,
                                               meta={"keep": i}))
        row.stylize(Style(meta={"row": i}))
        if i == cursor:                    # the bar stops short of the button, which keeps its color
            row.stylize(Style(bgcolor=CURSOR_BG), 0, button_at)
        t.append("  ")
        t.append_text(row)
        t.append("\n")
    below = entries[top + room:]
    if top or below:
        week = sum(1 for e in below if e.deletes_at - now < 7 * DAY)
        parts = [f"{top} above"] if top else []
        if week:
            parts.append(f"{week} more this week")
        if len(below) > week:
            parts.append(f"{len(below) - week} {'' if week else 'more '}next week")
        t.append("  + " + ", ".join(parts) + "  ·  scroll for them\n", style="dim")
    return t


def _home_short(path: str) -> str:
    home = os.path.expanduser("~")
    return "~" + path[len(home):] if path == home or path.startswith(home + "/") else path


def _cd_line(cwd: str) -> str:
    """`cd` to a folder as a shell reads it: ~ stays outside the quotes so it expands."""
    short = _home_short(cwd)
    if short.startswith("~/"):
        return "cd ~/" + shlex.quote(short[2:])
    return f"cd {shlex.quote(cwd)}"


def expiring_detail(e: Expiring, now: float, period: float | None, live: bool = False) -> Text:
    out = Text()
    period = period or DEFAULT_PERIOD_DAYS

    def kv(k: str, v: str, style: str = ""):
        out.append(f"{k:<10}", style="bold dim")
        out.append(f"{v}\n", style=style)

    out.append(e.title or "untitled", style="bold underline")
    out.append("\n")
    used = datetime.fromtimestamp(e.last_used)
    if e.kept_at:
        kv("status", f"kept {fmt_age(e.kept_at, now)} ago · safe until {_day_month(e.kept_at + period * DAY)}",
           GREEN)
    else:
        kv("status", f"{'open in a session, but quiet' if live else 'not running'} · last used "
                     f"{used:%a %b} {used.day} {used:%H:%M}, {fmt_age(e.last_used, now)} ago")
        at = datetime.fromtimestamp(e.deletes_at)
        stamp = f"{at:%a %b} {at.day} {at:%H:%M}"
        kv("expires", (f"expired {stamp}" if e.deletes_at <= now else stamp)
           + f", {period:g} days after its last use. Claude Code deletes it at its first cleanup after "
             "that (one runs a few minutes after any session starts); until then keep saves it.",
           YELLOW if e.soon(now) else "")
    kv("cwd", _home_short(e.cwd) or "(unknown)")
    kv("size", f"{_megabytes(e.size)} with its subagents and tool results")
    if not e.kept_at:
        kv("keep", f"k or its keep button gives it {period:g} more days: agent-farm marks the transcript, "
                   "its tool results and its checkpoints as just used. Nothing inside them changes.")
    kv("resume", (f"{_cd_line(e.cwd)} && " if e.cwd else "") + f"claude --resume {e.session_id}"
       + "  (resuming keeps it too)")
    return out


class ExpiringPane(Widget, can_focus=True):
    """The expiring pane. A click on a session's keep button keeps it; from the keyboard,
    tab here, pick one with the arrows and press k. The rows scroll when they do not all
    fit, and the detail pane follows the selected one while this pane has focus."""
    BINDINGS = [
        Binding("up", "move(-1)", "Up", show=False),
        Binding("down", "move(1)", "Down", show=False),
        Binding("k", "keep", "Keep"),
    ]

    class Highlighted(Message):
        """The selected session changed, or the pane gained or lost focus."""

    class Keep(Message):
        def __init__(self, entry: Expiring) -> None:
            super().__init__()
            self.entry = entry

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.entries: list[Expiring] = []
        self.period: float | None = DEFAULT_PERIOD_DAYS
        self.cursor_row = 0
        self.top_row = 0                 # the first session row shown

    @property
    def selected(self) -> Expiring | None:
        return self.entries[self.cursor_row] if 0 <= self.cursor_row < len(self.entries) else None

    def set_entries(self, entries: list[Expiring], period: float | None) -> None:
        """New scan results; the cursor stays on the same session when rows move."""
        sid = self.selected.session_id if self.selected else None
        self.entries, self.period = entries, period
        ids = [e.session_id for e in entries]
        self.cursor_row = ids.index(sid) if sid in ids else min(self.cursor_row, max(0, len(entries) - 1))
        self._follow_cursor()
        self.refresh()

    def _room(self) -> int:
        return expiry_room(self.content_size.height, len(self.entries))

    def _follow_cursor(self) -> None:
        room = self._room()
        if room <= 0:
            return
        if self.cursor_row < self.top_row:
            self.top_row = self.cursor_row
        elif self.cursor_row >= self.top_row + room:
            self.top_row = self.cursor_row - room + 1
        self.top_row = max(0, min(self.top_row, len(self.entries) - room))

    def render(self) -> Text:
        return expiring_text(self.entries, time.time(), self.content_size.width, self.content_size.height,
                             self.period, self.cursor_row if self.has_focus else None, self.top_row)

    def action_move(self, step: int) -> None:
        if self.entries:
            self.cursor_row = max(0, min(len(self.entries) - 1, self.cursor_row + step))
            self._follow_cursor()
            self.refresh()
            self.post_message(self.Highlighted())

    def action_keep(self) -> None:
        e = self.selected
        if e is not None and not e.kept_at:
            self.post_message(self.Keep(e))

    def on_click(self, event: events.Click) -> None:
        meta = event.style.meta
        if isinstance(meta.get("row"), int) and meta["row"] < len(self.entries):
            self.cursor_row = meta["row"]
            self.refresh()
            self.post_message(self.Highlighted())
        if isinstance(meta.get("keep"), int):
            self.action_keep()

    def _scroll(self, step: int) -> None:
        self.top_row = max(0, min(self.top_row + step, len(self.entries) - self._room()))
        self.refresh()

    def on_mouse_scroll_down(self, event: events.MouseScrollDown) -> None:
        self._scroll(1)
        event.stop()

    def on_mouse_scroll_up(self, event: events.MouseScrollUp) -> None:
        self._scroll(-1)
        event.stop()

    def on_focus(self) -> None:
        self.refresh()
        self.post_message(self.Highlighted())

    def on_blur(self) -> None:
        self.refresh()
        self.post_message(self.Highlighted())


def usage_text(scanner: UsageScanner, metric_idx: int, width: int,
               limits: LimitsReport | None = None, limits_on: bool = False, polling: bool = True) -> Text:
    key, label, col = USAGE_METRICS[metric_idx % len(USAGE_METRICS)]
    t = Text()
    t.append(f"Usage · last {USAGE_DAYS} days · {label}\n", style="bold")
    if not scanner.scanned_once:
        t.append("scanning transcripts…\n", style="dim")
        return t
    days = scanner.report(USAGE_DAYS)
    today = days[-1]
    t.append("today   ", style="dim")
    t.append(f"out {fmt_count(today.out_tokens)} · in {fmt_count(today.in_tokens)}\n")
    t.append("        ", style="dim")
    t.append(f"{today.messages} msgs · {today.session_count} sessions\n\n")
    t.append_text(limits_text(limits, limits_on, width, polling=polling))
    t.append("\n")

    vals = [getattr(d, key) for d in days]
    mx = max(vals) or 1
    label_w, val_w = 10, 6
    bar_w = max(6, width - label_w - 1 - 1 - val_w)
    # column header over the value column; the m hint sits at the left
    t.append("m: next measure".ljust(label_w + 1 + bar_w + 1), style="dim")
    t.append(f"{col:>{val_w}}\n", style="dim")

    prev_month = None
    for d, v in zip(days, vals):
        dt = datetime.strptime(d.date, "%Y-%m-%d")
        is_today = d is today
        is_max = v == mx and v > 0
        lab = f"{dt:%a %b %d}" if dt.month != prev_month else f"{dt:%a}     {dt:%d}"
        prev_month = dt.month
        t.append(f"{lab:<{label_w}} ", style="bold" if is_today else "dim")
        if v:
            t.append(_bar(v / mx, bar_w), style=BAR_HUE_TODAY if is_today else BAR_HUE)
        else:
            t.append("·".ljust(bar_w), style="dim")
        # recessed values: only today and the window maximum carry full ink
        t.append(f"{fmt_count(v):>{val_w}}\n", style="bold" if (is_today or is_max) else "dim")

    split = [(m, n) for m, n in scanner.model_split(USAGE_DAYS) if n > 0]
    if split:
        t.append("\n")
        t.append(f"output by model · {USAGE_DAYS} days\n", style="dim")
        t.append_text(model_split_text(split, width))
    cost = sum(d.cost_usd for d in days)
    if cost:
        t.append(f"logged cost ${cost:,.0f} · excludes today's sessions\n", style="dim")
    return t

def agent_fingerprint(inst: Instance) -> dict[str, tuple]:
    """What counts as a *material* update for a head's current-run subagents:
    a new agent, a status change, or a new message from the agent. Tool calls and
    archived agents are deliberately left out."""
    fp: dict[str, tuple] = {}
    for a in inst.agents:
        if a.archived:
            continue
        text = a.transcript.last_text if a.transcript else ""
        fp[a.agent_id] = (a.status, text[:300])
    if inst.status == "shell":            # waiting on its own command: a new one's row is news
        fp["shells"] = tuple(s.pid for s in shell_rows(inst.own_shells, time.time()))
    return fp


class SettingsScreen(Screen):
    """Full-screen settings. Changes apply immediately and are saved to disk."""
    BINDINGS = [
        Binding("escape", "close", "Back"),
        Binding("s", "close", "Back"),
        Binding("q", "close", "Back"),
    ]
    DEFAULT_CSS = """
    SettingsScreen { align: center top; }
    #settings { width: 90; max-width: 100%; height: auto; margin: 1 2; padding: 1 2;
                border: round $primary; }
    #settings .title { text-style: bold; margin-bottom: 1; }
    #settings .row { height: 3; }
    #settings .row Label { width: 1fr; padding: 1 1 0 0; }
    #settings .section { margin-top: 1; text-style: bold; color: $text-muted; }
    #settings RadioSet { border: none; padding: 0; }
    #settings .hint { color: $text-muted; margin-top: 1; }
    """

    def __init__(self, settings: Settings, on_change) -> None:
        super().__init__()
        self.settings = settings
        self.on_change = on_change

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="settings"):
            yield Static("Settings", classes="title")
            with Horizontal(classes="row"):
                yield Label("Show status text in rows  (state word and age; the triangle color says the same)")
                yield Switch(value=self.settings.show_status_text, id="show_status_text")
            with Horizontal(classes="row"):
                yield Label("Usage panel open at launch  (u toggles it any time)")
                yield Switch(value=self.settings.show_usage, id="show_usage")
            with Horizontal(classes="row"):
                yield Label(f"Align activity text  (pad names to a {self.settings.name_column_width}-column field)")
                yield Switch(value=self.settings.align_columns, id="align_columns")
            with Horizontal(classes="row"):
                yield Label("Plan limits in the usage panel  (what /usage shows; reads the Claude Code sign-in "
                            "from your Keychain, so macOS may ask you to allow it)")
                yield Switch(value=self.settings.show_limits, id="show_limits")
            with Horizontal(classes="row"):
                yield Label("Expiring pane  (the sessions Claude Code deletes next, each with a keep button; "
                            "it hides itself in a short window)")
                yield Switch(value=self.settings.show_expiring, id="show_expiring")
            yield Static("Ordering  (applies now and at launch)", classes="section")
            with RadioSet(id="ordering"):
                for o in ORDERINGS:
                    yield RadioButton(f"{o.name}  —  {o.description}", value=(o.name == self.settings.ordering))
            yield Static(f"Saved to {Settings.path().name} as you change them · esc returns to the monitor",
                         classes="hint")
        yield Footer()

    def on_switch_changed(self, event: Switch.Changed) -> None:
        if event.switch.id == "show_status_text":
            self.settings.show_status_text = event.value
        elif event.switch.id == "show_usage":
            self.settings.show_usage = event.value
        elif event.switch.id == "align_columns":
            self.settings.align_columns = event.value
        elif event.switch.id == "show_limits":
            self.settings.show_limits = event.value
        elif event.switch.id == "show_expiring":
            self.settings.show_expiring = event.value
        self._changed()

    def on_radio_set_changed(self, event: RadioSet.Changed) -> None:
        self.settings.ordering = ORDERINGS[event.index].name
        self._changed()

    def _changed(self) -> None:
        self.settings.save()
        log.info("settings changed: %s", self.settings)
        self.on_change()

    def action_close(self) -> None:
        self.app.pop_screen()


class AgentFarm(App):
    TITLE = "agent-farm"
    SUB_TITLE = f"v{VERSION}" if VERSION else ""       # the header reads "agent-farm — v0.2.4"
    CSS = """
    Screen { layout: vertical; }
    #body { height: 1fr; }
    #left { width: 1fr; height: 1fr; }
    #fleet { height: 1; padding: 0 1; }
    #tree { width: 1fr; height: 1fr; border: none; padding: 0 1; scrollbar-size-horizontal: 0; }
    /* The selected row keeps its colors (AgentTree.render_label), so the bar is a dark navy
       they all read on, not Textual's bright blue with its own text color. */
    #tree > .tree--cursor { background: #18283a; text-style: bold; }
    #tree:focus > .tree--cursor { background: #1f3b5c; text-style: bold; }
    #usage { width: 50; height: 1fr; border-left: solid $primary-darken-2; }
    #usage.hidden { display: none; }
    #usage-body { height: 1fr; padding: 0 1; overflow-y: auto; }
    #farm { padding: 0 1; border-top: solid $primary-darken-2; }
    #detail { height: 40%; min-height: 8; border-top: solid $primary-darken-2;
              padding: 0 1; overflow-y: auto; }
    #detail.hidden { display: none; }
    #expiring { padding: 0 1; border-top: solid $primary-darken-2; }
    #expiring.hidden { display: none; }
    """
    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("f", "focus_tab", "Bring forward"),
        Binding("d", "toggle_detail", "Detail"),
        Binding("u", "toggle_usage", "Usage"),
        Binding("m", "cycle_metric", "Metric", show=False),   # documented in the usage panel itself
        Binding("s", "settings", "Settings"),
        Binding("r", "refresh_now", "Refresh"),
    ]

    def __init__(self, order_name: str | None = None, show_usage: bool | None = None,
                 settings: Settings | None = None):
        super().__init__()
        self.log_path = setup_logging()          # first, so a settings warning has somewhere to go
        self.settings = settings or Settings.load()
        try:
            self.ordering: Ordering = get_ordering(order_name or self.settings.ordering)
        except KeyError:
            self.ordering = get_ordering(DEFAULT_ORDERING)
        if show_usage is None:
            show_usage = self.settings.show_usage
        self.collector = Collector()
        self.usage = UsageScanner()
        self.expiry = ExpiryScanner()
        self.limits = PlanLimits()
        self.usage_metric = 0
        # The usage panel shows when the setting or --usage says so, and `u` toggles it.
        self.usage_forced: bool = bool(show_usage)
        self._usage_width = 0
        self._cursor_pending = False
        self._agent_fp: dict[str, dict[str, tuple]] = {}   # head key -> subagent fingerprint
        self._strip_width = 0                               # agent strip column, recomputed per tick
        self._window_title: str | None = None               # last title sent to the terminal
        # Plan limits are fetched after work, not on a timer: _limits_worked latches any work seen
        # since the last fetch, and a fetch waits until LIMITS_POLL_S after the previous one.
        self._limits_worked = False
        self._working_now = False
        self._limits_last_try = float("-inf")              # _monotonic() of the last fetch started
        self._limits_drawn: tuple[float, bool] = (float("-inf"), False)   # (when, paused shown?) at last draw
        self.nodes: dict[str, TreeNode] = {}        # key -> node
        self.objects: dict[str, object] = {}        # key -> Instance | Agent
        self.detail_forced: bool | None = None      # None = auto by height
        self.last_snapshot_at = 0.0
        self.instances: list[Instance] = []
        logging.getLogger("agent_farm").addHandler(errors)
        log.info("monitor starting (pid %s)", os.getpid())

    def _handle_exception(self, error: Exception) -> None:
        """Textual swallows unhandled exceptions inside App.run(); it prints a traceback
        to the terminal and exits with return_code 1. Log it first so it survives."""
        log.error("unhandled exception in the UI, shutting down", exc_info=error)
        super()._handle_exception(error)

    # ---- layout -----------------------------------------------------------
    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="body"):
            with Vertical(id="left"):                   # the tree, the selected row, what expires
                yield Static(Text("starting…", style="dim"), id="fleet")
                tree: Tree = AgentTree("Claude instances", id="tree", look_lookup=self._look_for_key)
                tree.show_root = False
                tree.guide_depth = GUIDE_DEPTH
                yield tree
                yield Static("", id="detail")
                expiring = ExpiringPane(id="expiring")
                expiring.styles.height = BOTTOM_ROWS
                yield expiring
            with Vertical(id="usage"):                  # scrolls above; the farm chart stays put below
                yield Static("", id="usage-body")
                farm = Static("", id="farm")
                farm.styles.height = BOTTOM_ROWS        # as tall as the expiring pane: their tops meet
                yield farm
        yield Footer()

    def on_mount(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGHUP):
            try:
                loop.add_signal_handler(sig, self.exit)
            except (NotImplementedError, RuntimeError):
                pass
        self.query_one("#tree", Tree).focus()
        self._apply_detail_visibility()
        self._set_window_title(WINDOW_TITLE)
        self.collector.start()
        self.render_usage()
        self.collect()
        self.scan_usage()
        self.scan_expiring()
        self.set_interval(REFRESH_S, self.collect)
        self.set_interval(USAGE_SCAN_S, self.scan_usage)
        self.set_interval(EXPIRY_SCAN_S, self.scan_expiring)
        self.set_interval(FARM_REDRAW_S, self.render_farm)
        self._tick_limits(force=True)          # later fetches follow the work seen in each snapshot

    def on_resize(self) -> None:
        self._apply_detail_visibility()

    def _set_window_title(self, title: str) -> None:
        driver = self._driver
        if driver is None or title == self._window_title:   # no escape on every tick
            return
        try:
            driver.write(title_sequence(title))
            self._window_title = title
        except Exception:                 # headless, or already out of application mode
            log.debug("window title not set", exc_info=True)

    def exit(self, *args, **kwargs) -> None:
        self._set_window_title("")        # an iTerm2 tab gets its own title back
        super().exit(*args, **kwargs)

    def _apply_detail_visibility(self) -> None:
        show = self.detail_forced if self.detail_forced is not None else self.size.height >= SHORT_ROWS
        self.query_one("#detail", Static).set_class(not show, "hidden")
        show_u = self.usage_forced
        usage = self.query_one("#usage")
        usage.set_class(not show_u, "hidden")
        if show_u and usage.size.width and usage.size.width != self._usage_width:
            self.render_usage()
        # The expiring pane follows its setting, and steps aside in a window too short to
        # leave the tree room.
        show_x = self.settings.show_expiring and self.size.height >= SHORT_ROWS + BOTTOM_ROWS
        pane = self.query_one("#expiring", ExpiringPane)
        if pane.has_class("hidden") == show_x:
            pane.set_class(not show_x, "hidden")
            if show_x:
                self.render_expiring()
            elif pane.has_focus:
                self.query_one("#tree", Tree).focus()

    # ---- data -------------------------------------------------------------
    def _on_ui(self, fn, *args, **kwargs) -> None:
        """Run fn on the UI thread from a worker. A worker that finishes after q finds the
        app gone and call_from_thread raises "App is not running"; that is not an error."""
        try:
            self.call_from_thread(fn, *args, **kwargs)
        except RuntimeError as e:
            if "not running" not in str(e):
                raise
            log.debug("dropped a UI update after shutdown: %s", getattr(fn, "__name__", fn))

    @work(thread=True, exclusive=True, group="collect")
    def collect(self) -> None:
        try:
            snap = self.collector.snapshot()
        except Exception:        # keep the UI alive; details go to the log, not the screen
            log.exception("snapshot failed")
            self._on_ui(self._set_status, f"collector error — see {self.log_path}")
            return
        if snap is None:         # the previous tick is still running; this one skips its turn
            return
        try:
            self._on_ui(self.apply_snapshot, snap)
        except Exception:
            log.exception("apply_snapshot failed")

    @work(thread=True, exclusive=True, group="usage")
    def scan_usage(self) -> None:
        try:
            changed = self.usage.scan()
        except Exception:
            log.exception("usage scan failed")
            return
        if changed or not self._usage_width:
            self._on_ui(self.render_usage)

    @work(thread=True, exclusive=True, group="expiring")
    def scan_expiring(self) -> None:
        """Also on a timer while the pane is on: "expires today" and the chart move with the
        clock, and a kept session's green row goes after a minute."""
        if not self.settings.show_expiring:
            return
        try:
            self.expiry.scan()
        except Exception:
            log.exception("expiring scan failed")
            return
        self._on_ui(self.render_expiring)

    @work(thread=True, group="keep")
    def keep_session(self, entry: Expiring) -> None:
        try:
            self.expiry.keep(entry)
            self.expiry.scan()
        except Exception:
            log.exception("keep failed")
        self._on_ui(self.render_expiring)

    def render_expiring(self) -> None:
        try:
            pane = self.query_one("#expiring", ExpiringPane)
        except NoMatches:          # another screen is up; the next scan catches up
            return
        pane.set_entries(self.expiry.entries(), self.expiry.period_days)
        if pane.has_focus:
            self._refresh_detail()

    def on_expiring_pane_highlighted(self, message: ExpiringPane.Highlighted) -> None:
        self._refresh_detail()

    def on_expiring_pane_keep(self, message: ExpiringPane.Keep) -> None:
        self.keep_session(message.entry)

    def _limits_polling(self) -> bool:
        """Will a fetch follow? Only once something has worked since the last one: the
        numbers cannot move while everyone is idle or waiting for input."""
        return self._limits_worked or self._working_now

    def _limits_on_snapshot(self, instances: list[Instance]) -> None:
        """Called with every snapshot (about once a second): remember any work, then fetch if
        it is due. Checking every snapshot instead of on a fixed timer means the first fetch
        after a pause comes as soon as work resumes, and a short burst between two checks
        is still counted."""
        self._working_now = fleet_working(instances)
        if self._working_now:
            self._limits_worked = True
        self._tick_limits()
        drawn_at, drawn_polling = self._limits_drawn
        if (self.is_running and self.settings.show_limits and self.usage_forced and
                (self._limits_polling() != drawn_polling or _monotonic() - drawn_at >= LIMITS_REDRAW_S)):
            self.render_usage()

    def _tick_limits(self, force: bool = False) -> None:
        """Fetch plan limits only when someone can see them (panel open, setting on), and then
        only when work happened since the last fetch and that fetch is LIMITS_POLL_S old.
        Opening the panel or turning the setting on fetches at once (force), so the block
        never starts empty."""
        if not (self.settings.show_limits and self.usage_forced):
            return
        due = self._limits_worked and _monotonic() - self._limits_last_try >= LIMITS_POLL_S
        if force or due:
            self._limits_last_try = _monotonic()
            self._limits_worked = False                     # the next snapshot re-arms it if work goes on
            self.fetch_limits()

    @work(thread=True, exclusive=True, group="limits")
    def fetch_limits(self) -> None:
        try:
            changed = self.limits.refresh()
        except Exception:
            log.exception("plan limits fetch failed")
            return
        if changed:
            self._on_ui(self.render_usage)

    def _usage_text_width(self) -> int:
        """Columns for text in the usage column: its width inside the border, less padding."""
        usage = self.query_one("#usage")
        return max(30, (usage.size.width or USAGE_PANEL_WIDTH - 1) - 2)

    def render_usage(self) -> None:
        try:
            usage = self.query_one("#usage")
            body = self.query_one("#usage-body", Static)
        except NoMatches:
            return
        width = self._usage_text_width()
        self._usage_width = usage.size.width
        self._limits_drawn = (_monotonic(), self._limits_polling())
        body.update(usage_text(self.usage, self.usage_metric, width,
                               self.limits.report(), self.settings.show_limits, self._limits_polling()))
        self.render_farm()

    def render_farm(self) -> None:
        """The agents-at-work chart. Also on a timer: the current hour fills in and the
        hours shift along even when no transcript changes."""
        try:
            usage = self.query_one("#usage")
            farm = self.query_one("#farm", Static)
        except NoMatches:
            return
        if usage.has_class("hidden") or not self.usage.scanned_once:
            return
        # A background command whose shell is still running keeps its agent at work up to now.
        live = {i.session_id: [s.started for s in i.shells] for i in self.instances if i.alive and i.shells}
        farm.update(activity_text(self.usage.activity(live_shells=live), self._usage_text_width()))

    def action_toggle_usage(self) -> None:
        usage = self.query_one("#usage")
        self.usage_forced = usage.has_class("hidden")      # hidden -> show, shown -> hide
        self._apply_detail_visibility()
        if self.usage_forced:
            self.render_usage()
            self._tick_limits(force=True)

    def action_cycle_metric(self) -> None:
        self.usage_metric = (self.usage_metric + 1) % len(USAGE_METRICS)
        self.render_usage()

    def _set_status(self, msg, tooltip: str | None = None) -> None:
        """Update the fleet line (used for the rendered summary and for terse error notes)."""
        try:
            fleet = self.query_one("#fleet", Static)
        except NoMatches:          # app is shutting down, or another screen is up
            return
        fleet.update(msg)
        if tooltip is not None:
            fleet.tooltip = tooltip

    # ---- tree building ------------------------------------------------------
    def _look_for_key(self, key: str) -> Look | None:
        return look_of(self.objects.get(key))

    def _name_width(self, depth: int, tree: Tree | None = None) -> int:
        """Name column so that activity text starts in the same column on every row.
        A head row is: icon(2) + name + space + strip(w). A child row at `depth` is:
        guides(depth*guide_depth) + icon(2) + name, so its name column absorbs the difference."""
        if not self.settings.align_columns:
            return 0
        base = max(8, self.settings.name_column_width)
        if depth == 0:                       # head rows render the strip separately
            return base
        guide = (tree.guide_depth if tree is not None else GUIDE_DEPTH)
        strip = (self._strip_width + 1) if self._strip_width else 0
        return max(8, base + strip - depth * guide)

    @staticmethod
    def _ikey(inst: Instance) -> str:
        return f"i:{inst.pid}"          # pid, not session id: a stale registry entry can share one

    def _upsert_instance(self, tree: Tree, inst: Instance, now: float, width: int,
                         seen: set[str], expanded: dict[str, bool] | None = None) -> TreeNode:
        """Add or refresh one head-agent row and everything under it, in a fixed order: its
        own shells, its current-run subagents (each with its shells), the archive last. A
        new row is inserted at its place, so rows already there never move. `expanded`
        holds fold states to restore on a rebuild."""
        expanded = expanded or {}
        key = self._ikey(inst)
        seen.add(key)
        self.objects[key] = inst
        node = self.nodes.get(key)
        label = instance_label(inst, now, self.settings.show_status_text, self._name_width(0),
                               self._strip_width)
        label.truncate(width, overflow="ellipsis")
        if node is None:
            node = tree.root.add(label, data=key, expand=expanded.get(key, True))
            self.nodes[key] = node
        else:
            node.set_label(label)
        current = [a for a in inst.agents if not a.archived]
        archived = [a for a in inst.agents if a.archived]
        own = shell_rows(inst.own_shells, now)
        node.allow_expand = bool(current or archived or own)
        gkey = f"{key}/archived"
        group = self.nodes.get(gkey)
        if group is not None and group.parent is not node:
            group = None

        def place(parent: TreeNode, stale: TreeNode | None) -> TreeNode | None:
            """Drop a node whose row moved to another parent; the caller re-creates it."""
            if stale is not None and stale.parent is not parent:
                try:
                    stale.remove()
                except Exception:
                    pass
                return None
            return stale

        def upsert_shell(sh: Shell, parent: TreeNode, pkey: str, depth: int,
                         before: TreeNode | None = None) -> None:
            skey = f"{pkey}/s:{sh.pid}"
            seen.add(skey)
            self.objects[skey] = sh
            slabel = shell_label(sh, now, self._name_width(depth, tree))
            slabel.truncate(width - (depth + 1) * tree.guide_depth, overflow="ellipsis")
            snode = place(parent, self.nodes.get(skey))
            if snode is None:
                self.nodes[skey] = parent.add_leaf(slabel, data=skey, before=before)
            else:
                snode.set_label(slabel)

        def upsert_agent(a: Agent, parent: TreeNode, depth: int, before: TreeNode | None = None) -> None:
            akey = f"{key}/a:{a.agent_id}"
            seen.add(akey)
            self.objects[akey] = a
            alabel = agent_label(a, now, self.settings.show_status_text, self._name_width(depth, tree))
            alabel.truncate(width - 2 * tree.guide_depth, overflow="ellipsis")
            rows = shell_rows(a.shells, now)            # none for an earlier run's agents
            anode = place(parent, self.nodes.get(akey))     # None too when it moved to the archive
            if anode is None:
                anode = parent.add(alabel, data=akey, before=before, expand=expanded.get(akey, True),
                                   allow_expand=bool(rows))
                self.nodes[akey] = anode
            else:
                anode.set_label(alabel)
                if rows and not anode.allow_expand:
                    anode.expand()               # its first shell: show it, even if folded before
                anode.allow_expand = bool(rows)
            for sh in rows:
                upsert_shell(sh, anode, akey, depth + 1)

        # 1) the head's own shells, ahead of its subagents
        first_agent = next((c for c in node.children if "/a:" in str(c.data)), None)
        for sh in own:
            upsert_shell(sh, node, key, 1, before=first_agent or group)
        # 2) current-run subagents, ahead of the archive
        for a in current:
            upsert_agent(a, node, 1, before=group)
        # 3) the archived accordion, always last
        if archived:
            glabel = Text()
            dark = AGENT_LOOK["archived"]
            glabel.append(f"archived ({len(archived)})", style=_styled("bold", dark.name))
            glabel.append("  ")
            stopped = sum(1 for a in archived if a.stopped)
            why = ("earlier runs of this session" if not stopped else "stopped over a day ago"
                   if stopped == len(archived) else "earlier runs, or stopped over a day ago")
            glabel.append(f"{why} · logs only", style=_styled("italic", dark.summary))
            glabel.truncate(width - tree.guide_depth, overflow="ellipsis")
            if group is None:
                group = node.add(glabel, data=gkey, expand=expanded.get(gkey, False))
                self.nodes[gkey] = group
            else:
                group.set_label(glabel)
            seen.add(gkey)
            self.objects[gkey] = ArchiveGroup(inst, archived)
            for a in archived:
                upsert_agent(a, group, 2)
        # (a group with nothing left in it, and rows that ended, go in the stale-key sweep)
        return node

    def _auto_expand_on_updates(self, instances: list[Instance]) -> None:
        for inst in instances:
            key = self._ikey(inst)
            fp = agent_fingerprint(inst)
            prev = self._agent_fp.get(key)
            self._agent_fp[key] = fp
            if prev is None:                      # first sighting: the row's default applies
                continue
            changed = [aid for aid, v in fp.items() if prev.get(aid) != v]
            if not changed:
                continue
            node = self.nodes.get(key)
            if node is not None and node.allow_expand and not node.is_expanded:
                node.expand()
                log.info("auto-expanded %s: subagent update from %s", inst.name or inst.pid, ", ".join(changed))

    def _rebuild_tree(self, tree: Tree, instances: list[Instance], now: float, width: int) -> None:
        """Re-create the rows in `instances` order, keeping the cursor on the same
        instance and each row's fold state. Only called when the order changed."""
        cursor_key = tree.cursor_node.data if tree.cursor_node is not None else None
        expanded = {k: n.is_expanded for k, n in self.nodes.items() if not is_shell_key(k)}
        tree.clear()
        self.nodes.clear()
        self.objects.clear()
        seen: set[str] = set()
        for inst in instances:
            self._upsert_instance(tree, inst, now, width, seen, expanded)
        if not tree.root.is_expanded:
            tree.root.expand()
        target = self.nodes.get(cursor_key) if cursor_key else None
        if target is None and tree.root.children:
            target = tree.root.children[0]
        if target is not None:
            # Node line numbers are computed lazily on the next refresh, so moving the
            # cursor right now would land on line -1. Defer until the tree has laid out.
            self._cursor_pending = True

            def _restore(node: TreeNode = target) -> None:
                self._cursor_pending = False
                try:
                    tree.move_cursor(node)
                except Exception:
                    try:
                        tree.cursor_line = 0
                    except Exception:
                        pass
                self._refresh_detail()

            self.call_after_refresh(_restore)
        log.debug("tree rebuilt for ordering %s", self.ordering.name)

    def apply_snapshot(self, instances: list[Instance]) -> None:
        now = time.time()
        instances = self.ordering.sort(instances)
        self.instances = instances
        self.last_snapshot_at = now
        try:
            tree = self.query_one("#tree", Tree)
        except NoMatches:          # a snapshot landed after the screen was torn down
            return
        self._apply_detail_visibility()
        width = max(30, tree.size.width - 4)        # labels are clipped to fit, no h-scroll
        self._strip_width = strip_column_width(instances)

        # 1) refresh labels in place, adding new rows at the end
        seen: set[str] = set()
        for inst in instances:
            self._upsert_instance(tree, inst, now, width, seen)

        # 2) drop rows for instances / agents that disappeared
        for key in list(self.nodes):
            if key not in seen:
                try:
                    self.nodes[key].remove()
                except Exception:
                    pass
                self.nodes.pop(key, None)
                self.objects.pop(key, None)

        # 3) if the visible order no longer matches the ordering, rebuild once
        desired = [self._ikey(i) for i in instances]
        current = [n.data for n in tree.root.children]
        if current != desired:
            self._rebuild_tree(tree, instances, now, width)

        # 4) auto-expand a head whose current-run subagents had a material update.
        #    The archived accordion is never opened programmatically.
        self._auto_expand_on_updates(instances)

        if tree.cursor_node is None and tree.root.children and not self._cursor_pending:
            try:
                tree.cursor_line = 0                              # highlight the first row on launch
            except Exception:
                pass

        recent = errors.recent(ERROR_WINDOW_S)
        self._set_status(fleet_line(instances, self.settings.show_status_text,
                                    recent, self.collector.iterm.available),
                         fleet_tooltip(instances, self.ordering, recent, self.collector.iterm.available))
        self._set_window_title(window_title(instances))
        self._limits_on_snapshot(instances)
        self._refresh_detail()

    # ---- detail pane ------------------------------------------------------
    def on_tree_node_highlighted(self, event: Tree.NodeHighlighted) -> None:
        self._refresh_detail(event.node)

    def _refresh_detail(self, node: TreeNode | None = None) -> None:
        try:
            tree = self.query_one("#tree", Tree)
            detail = self.query_one("#detail", Static)
            pane = self.query_one("#expiring", ExpiringPane)
        except NoMatches:
            return
        if pane.has_focus:                     # the detail pane follows whichever list has focus
            e = pane.selected
            live = e is not None and any(i.alive and i.session_id == e.session_id for i in self.instances)
            detail.update(expiring_detail(e, time.time(), pane.period, live) if e
                          else Text("nothing expires in the next two weeks", style="dim"))
            return
        node = node or tree.cursor_node
        if node is None or node.data is None:
            detail.update(Text("select an instance", style="dim"))
            return
        obj = self.objects.get(node.data)
        detail.update(detail_text(obj, time.time()) if obj else Text(""))

    # ---- actions ----------------------------------------------------------
    def action_settings(self) -> None:
        if isinstance(self.screen, SettingsScreen):
            return
        self.push_screen(SettingsScreen(self.settings, self._on_settings_changed))

    def _on_settings_changed(self) -> None:
        """Apply a settings change right away; the next tick would catch it anyway."""
        if self.settings.ordering != self.ordering.name:
            try:
                self.ordering = get_ordering(self.settings.ordering)
            except KeyError:
                pass
        self.render_usage()                    # the plan-limits block follows its setting at once
        self._tick_limits(force=True)
        self.scan_expiring()                   # the pane shows on return with a fresh list
        # Labels are rebuilt from the current instances on the main screen's next apply.
        self.call_later(lambda: self.apply_snapshot(list(self.instances)) if self.instances else None)

    def action_toggle_detail(self) -> None:
        detail = self.query_one("#detail", Static)
        self.detail_forced = detail.has_class("hidden")   # hidden -> force show, shown -> force hide
        self._apply_detail_visibility()

    def action_refresh_now(self) -> None:
        self.collect()            # the tab map refreshes on its own thread; a tick is enough

    def action_focus_tab(self) -> None:
        tree = self.query_one("#tree", Tree)
        node = tree.cursor_node
        if node is None or node.data is None:
            return
        key = node.data.split("/")[0]
        inst = self.objects.get(key)
        if not isinstance(inst, Instance):
            return
        label = f"{inst.name or inst.pid} (pid {inst.pid}, {'Claude app' if inst.is_app else 'tty ' + (inst.tty or '?')})"
        log.info("focus requested for %s -> iTerm session %r title %r", label, inst.iterm_session, inst.iterm_title)
        self._focus_in_background(inst, label)

    @work(thread=True, group="focus")
    def _focus_in_background(self, inst: Instance, label: str) -> None:
        """it2 and open can wait seconds on iTerm2; never on the thread that draws."""
        ok, why = self.collector.focus_instance(inst, label)
        if ok:
            msg = "brought the Claude app forward" if inst.is_app else f"focused {inst.iterm_title or inst.name}"
            self._on_ui(self.notify, msg, timeout=2)
        else:
            self._on_ui(self.notify, f"focus failed: {why[:60]} — see the log",
                        severity="warning", timeout=4)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Monitor the Claude Code instances on this Mac.")
    env = os.environ
    ap.add_argument("--order", default=env.get("AGENT_FARM_ORDER") or env.get("CLAUDE_MONITOR_ORDER") or None,
                    choices=[o.name for o in ORDERINGS],
                    help="initial instance ordering for this run (default: the saved setting)")
    ap.add_argument("--usage", action="store_true",
                    default=(env.get("AGENT_FARM_USAGE") or env.get("CLAUDE_MONITOR_USAGE") or "") not in ("", "0", "false"),
                    help="start with the usage panel shown (default: the saved setting)")
    args = ap.parse_args()
    app = AgentFarm(order_name=args.order, show_usage=True if args.usage else None)
    try:
        app.run()
    except Exception:            # errors outside Textual's own handling (startup, driver)
        log.exception("monitor crashed")
        raise
    finally:
        log.info("monitor exiting (return code %s)", app.return_code)
    sys.exit(app.return_code or 0)   # so the supervisor sees a crash as a non-zero exit
