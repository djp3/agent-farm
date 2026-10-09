"""
agent-farm — a Textual TUI that watches every Claude Code instance on this Mac.

Head agents are top-level rows; subagents / teammates nest beneath them.
Head agents are the tree rows; the detail pane for the highlighted row sits underneath,
and a usage panel (tokens per day across every transcript) sits to the right of the tree.
Keys: ↑/↓ move · enter/space fold · f focus iTerm2 tab · d detail · u usage · m metric · s settings · r refresh · q quit
Instances are ordered by the Ordering chosen in Settings (see ordering.py); `--order NAME` overrides for one run.
"""
from __future__ import annotations

import asyncio
import logging
import re
import signal
import sys
import time
from datetime import datetime

from rich.style import Style
from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.css.query import NoMatches
from textual.screen import Screen
from textual.widgets import (Footer, Header, Label, RadioButton, RadioSet, Static, Switch,
                             Tree)
from textual.widgets.tree import TreeNode

from collector import (Agent, Collector, ErrorCounter, Instance, fmt_age, fmt_tokens,
                       setup_logging)
from ordering import DEFAULT_ORDERING, ORDERINGS, Ordering, get_ordering
from settings import Settings
from usage import UsageScanner, fmt_count

log = logging.getLogger("monitor.app")
errors = ErrorCounter()

REFRESH_S = 1.0
USAGE_SCAN_S = 10.0
SHORT_ROWS = 22        # below this many rows the detail pane hides itself
NARROW_COLS = 110      # below this many columns the usage panel hides itself
USAGE_DAYS = 14
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

# Status palette. The four head-session colors are the ones iTerm2 paints on its own
# Claude status dot, so the tab bar and this monitor agree. Rows carry the color on
# their triangle; the detail pane spells the state out.
STATUS_STYLE = {
    "waiting": "bold #ff9500",        # needs you: permission prompt, question, plan approval
    "busy": "#00d75f",                # working on your input
    "idle": "#5f87ff",                # finished, ready for your next instruction
    "dead": "#888888", "unknown": "#888888",       # inactive: stale registry entry / unregistered
    "running": "#00d75f",             # subagent working
    "starting": "dim #00d75f",        # spawned, no transcript yet
    "dormant": "dim #5f87ff",         # finished, resident, can be messaged again
    "stale": "#d75fd7",               # silent too long mid tool-call, likely killed
    "archived": "dim #888888",        # log only
    "error": "#ff5f5f",               # the collector failed on this row (see logs/monitor.log)
}


TOGGLE_META = Style.from_meta({"toggle": True})     # lets a mouse click on the glyph fold the row


class AgentTree(Tree):
    """One glyph per row: ▶ / ▼ when the row has children to fold, ▷ when it has none.
    The glyph is colored by the row's status, so state stays visible at a glance."""
    ICON_NODE = "▶ "
    ICON_NODE_EXPANDED = "▼ "
    ICON_LEAF = "▷ "

    def __init__(self, *args, status_lookup=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.status_lookup = status_lookup or (lambda key: "")

    def render_label(self, node: TreeNode, base_style: Style, style: Style) -> Text:
        label = node._label.copy()
        label.stylize(style)
        if node.allow_expand:
            icon = self.ICON_NODE_EXPANDED if node.is_expanded else self.ICON_NODE
            icon_style = base_style + TOGGLE_META
        else:
            icon = self.ICON_LEAF
            icon_style = base_style
        status = self.status_lookup(node.data) if node.data is not None else ""
        if status:
            try:
                icon_style = icon_style + Style.parse(STATUS_STYLE.get(status, ""))
            except Exception:
                pass
        return Text.assemble((icon, icon_style), label)


STRIP_MAX = 16                       # cap on the agent strip column (16 asterisks = 80 agents)
STRIP_ORDER = ("running", "starting", "stale", "dormant")
STRIP_CIRCLE = {"running": "●", "starting": "◌", "stale": "◍", "dormant": "○"}
STRIP_FIVE = "✱"                      # one asterisk tallies five agents of that state


def agent_glyphs(agents: list[Agent]) -> list[tuple[str, str]]:
    """One circle per current-run subagent, one asterisk per five, colored by state and
    ordered running → starting → stale → dormant. Filled means working, hollow means
    finished, so it reads without color."""
    counts = {st: 0 for st in STRIP_ORDER}
    for a in agents:
        if not a.archived and a.status in counts:
            counts[a.status] += 1
    glyphs: list[tuple[str, str]] = []
    for st in STRIP_ORDER:
        n = counts[st]
        style = STATUS_STYLE.get(st, "")
        glyphs += [(STRIP_FIVE, style)] * (n // 5)
        glyphs += [(STRIP_CIRCLE[st], style)] * (n % 5)
    if len(glyphs) > STRIP_MAX:
        glyphs = glyphs[:STRIP_MAX - 1] + [("+", "dim")]
    return glyphs


def strip_column_width(instances: list[Instance]) -> int:
    """The strip column is exactly as wide as the widest strip on screen right now."""
    return max((len(agent_glyphs(i.agents)) for i in instances), default=0)


def agent_strip(agents: list[Agent], width: int) -> Text:
    glyphs = agent_glyphs(agents)
    t = Text()
    for g, st in glyphs:
        t.append(g, style=st)
    t.pad_right(width - len(glyphs))
    return t


HEAD_GLYPH = "■"                      # one square per head session in the fleet line
HEAD_ORDER = ("waiting", "busy", "idle", "dead", "unknown")


def fleet_line(instances: list[Instance], ordering: Ordering, now: float, show_text: bool,
               error_count: int, it2_ok: bool) -> Text:
    """The one-line summary above the tree: a square per head session grouped and colored
    by state (waiting → busy → idle → inactive), the fleet-wide subagent tally in the same
    circles-and-asterisks system as the rows, then order, it2 and clock in dim text.
    With the status-text setting on, the counts are also spelled out."""
    live = [i for i in instances if i.alive]
    t = Text()
    for st in HEAD_ORDER:
        n = sum(1 for i in instances if (i.status == st and (i.alive or st in ("dead", "unknown"))))
        if n:
            t.append(HEAD_GLYPH * n, style=STATUS_STYLE.get(st, ""))
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
        running = sum(1 for a in all_agents if a.status == "running")
        t.append("  ")
        t.append(f"{len(live)} sessions · {busy} busy · {waiting} waiting · {running} agents running")
    if not it2_ok:
        t.append("  ")
        t.append("it2 missing — focus (f) unavailable", style=STATUS_STYLE["error"])
    if error_count:                      # recent problems only; the tooltip has the total
        t.append("  ")
        t.append(f"⚠ {error_count} (logs/monitor.log)", style=STATUS_STYLE["error"])
    return t


def fleet_tooltip(instances: list[Instance], ordering: Ordering, error_count: int, it2_ok: bool) -> str:
    """Everything the fleet line encodes, as words (shown on mouse hover)."""
    live = [i for i in instances if i.alive]
    counts = {st: sum(1 for i in live if i.status == st) for st in ("waiting", "busy", "idle")}
    inactive = len(instances) - len(live) + sum(1 for i in live if i.status == "unknown")
    agents = [a for i in live for a in i.agents if not a.archived]
    ag = {st: sum(1 for a in agents if a.status == st) for st in ("running", "starting", "stale", "dormant")}
    archived = sum(1 for i in instances for a in i.agents if a.archived)
    lines = [
        f"{len(instances)} Claude session{'s' if len(instances) != 1 else ''}: "
        f"{counts['waiting']} waiting for you, {counts['busy']} busy, {counts['idle']} idle, {inactive} inactive",
        "  one ■ per session, colored orange / green / blue / grey in that order",
        f"{len(agents)} current-run subagent{'s' if len(agents) != 1 else ''}: "
        f"{ag['running']} running, {ag['starting']} starting, {ag['stale']} stale, {ag['dormant']} dormant"
        + (f"  (+{archived} archived, not shown)" if archived else ""),
        "  ● running  ◌ starting  ◍ stale  ○ dormant · ✱ = five of a kind",
        f"ordering: {ordering.name} — {ordering.description}",
    ]
    if not it2_ok:
        lines.append("iTerm2's it2 CLI was not found, so the f key cannot focus tabs")
    if error_count:
        lines.append(f"{error_count} warning{'s' if error_count != 1 else ''} in the last 10 minutes "
                     f"({errors.count} since launch) — details in logs/monitor.log")
    return "\n".join(lines)


def _fit_name(name: str, width: int) -> str:
    if width <= 0:
        return name
    if len(name) > width:
        return name[: width - 1] + "…"
    return name.ljust(width)


def _status_words(status: str, since: float | None, now: float) -> Text:
    t = Text("  ")
    t.append(status, style=STATUS_STYLE.get(status, ""))
    if since:
        t.append(" ")
        t.append(fmt_age(since, now), style="dim")
    return t


def instance_label(i: Instance, now: float, show_status: bool = False, name_width: int = 0,
                   strip_width: int = 0) -> Text:
    """Row = name · agent strip · activity. The name comes first so subagent rows, indented
    by the tree guides, visibly hang under their lead's name; the strip sits in its own
    aligned column right after the name field."""
    t = Text()
    name = i.name or f"pid {i.pid}"
    if i.is_app:                              # runs in the Claude desktop app, not a terminal:
        t.append(_fit_name(name, name_width - 2 if name_width else 0), style="bold")
        t.append(" ⧉", style="cyan")          # the marker survives name truncation
    else:
        t.append(_fit_name(name, name_width), style="bold")
    if strip_width:
        t.append(" ")
        t.append_text(agent_strip(i.agents, strip_width))
    if show_status:
        t.append_text(_status_words(i.status, i.status_since, now))
    act = i.activity
    if act:
        t.append("  ")
        t.append(act, style="italic" if i.status != "waiting" else "yellow")
    return t


def agent_label(a: Agent, now: float, show_status: bool = False, name_width: int = 0) -> Text:
    t = Text()
    name = a.name + (f" ({a.agent_type})" if a.agent_type and a.agent_type != a.name else "")
    t.append(_fit_name(name, name_width), style=f"bold {a.color}" if a.color else "bold")
    if show_status:
        t.append_text(_status_words(a.status, a.last_ts, now))
    act = a.activity
    if act:
        t.append("  ")
        t.append(act, style="italic")
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


def detail_text(obj, now: float, ordering: Ordering | None = None) -> Text:
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
        kv("status", f"{i.status} for {fmt_age(i.status_since, now)}"
           + (f"  ({i.status_detail})" if i.status_detail else ""),
           STATUS_STYLE.get(i.status, ""))
        if i.is_app:
            kv("where", "Claude desktop app  (f brings the app forward)", "cyan")
        kv("cwd", i.cwd or "(unknown)")
        if i.git and i.git.get("error"):
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
        out.append("\nThese ran in an earlier process of this session. Teammates live inside the\n"
                   "head process, so they were not restarted on resume; only their transcripts\n"
                   "remain. Expand the row to browse them.\n", style="dim")
    elif isinstance(obj, Agent):
        a = obj
        out.append(a.name, style=f"bold underline {a.color}" if a.color else "bold underline")
        out.append("\n")
        kv("status", f"{a.status} for {fmt_age(a.last_ts, now)}"
           + ("  (log only, from an earlier run)" if a.archived else ""),
           STATUS_STYLE.get(a.status, ""))
        kv("type", a.agent_type)
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
            if tr.last_text:
                out.append("last message\n", style="bold dim")
                out.append(tr.last_text.strip()[:1500] + "\n")
    return out


def _bar(frac: float, width: int) -> str:
    cells = max(0.0, min(1.0, frac)) * width
    full = int(cells)
    s = "█" * full
    if full < width:
        eighth = int(round((cells - full) * 8))
        if eighth:
            s += BAR_EIGHTHS[eighth]
    return s.ljust(width)


def usage_text(scanner: UsageScanner, metric_idx: int, width: int) -> Text:
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


class ClaudeMonitor(App):
    TITLE = "agent-farm"
    CSS = """
    Screen { layout: vertical; }
    #body { height: 1fr; }
    #top { height: 1fr; }
    #left { width: 1fr; height: 1fr; }
    #fleet { height: 1; padding: 0 1; }
    #tree { width: 1fr; height: 1fr; border: none; padding: 0 1; scrollbar-size-horizontal: 0; }
    #usage { width: 50; height: 1fr; border-left: solid $primary-darken-2; padding: 0 1; overflow-y: auto; }
    #usage.hidden { display: none; }
    #detail { width: 1fr; height: 40%; min-height: 8; border-top: solid $primary-darken-2;
              padding: 0 1; overflow-y: auto; }
    #detail.hidden { display: none; }
    """
    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("f", "focus_tab", "Focus iTerm tab"),
        Binding("d", "toggle_detail", "Detail"),
        Binding("u", "toggle_usage", "Usage"),
        Binding("m", "cycle_metric", "Metric", show=False),   # documented in the usage panel itself
        Binding("s", "settings", "Settings"),
        Binding("r", "refresh_now", "Refresh"),
    ]

    def __init__(self, order_name: str | None = None, show_usage: bool | None = None,
                 settings: Settings | None = None):
        super().__init__()
        self.settings = settings or Settings.load()
        try:
            self.ordering: Ordering = get_ordering(order_name or self.settings.ordering)
        except KeyError:
            self.ordering = get_ordering(DEFAULT_ORDERING)
        if show_usage is None:
            show_usage = self.settings.show_usage
        self.collector = Collector()
        self.usage = UsageScanner(interval_s=USAGE_SCAN_S)
        self.usage_metric = 0
        # The usage panel is hidden until `u` is pressed (or --usage at launch).
        # True/False = user's choice; None = auto by terminal width.
        self.usage_forced: bool | None = True if show_usage else False
        self._usage_width = 0
        self._cursor_pending = False
        self._agent_fp: dict[str, dict[str, tuple]] = {}   # head key -> subagent fingerprint
        self._strip_width = 0                               # agent strip column, recomputed per tick
        self.nodes: dict[str, TreeNode] = {}        # key -> node
        self.objects: dict[str, object] = {}        # key -> Instance | Agent
        self.detail_forced: bool | None = None      # None = auto by width
        self.last_snapshot_at = 0.0
        self.instances: list[Instance] = []
        self.log_path = setup_logging()
        logging.getLogger("monitor").addHandler(errors)
        log.info("monitor starting (pid %s)", __import__("os").getpid())

    def _handle_exception(self, error: Exception) -> None:
        """Textual swallows unhandled exceptions inside App.run(); it prints a traceback
        to the terminal and exits with return_code 1. Log it first so it survives."""
        log.error("unhandled exception in the UI, shutting down", exc_info=error)
        super()._handle_exception(error)

    # ---- layout -----------------------------------------------------------
    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Vertical(id="body"):
            with Horizontal(id="top"):
                with Vertical(id="left"):
                    yield Static(Text("starting…", style="dim"), id="fleet")
                    tree: Tree = AgentTree("Claude instances", id="tree", status_lookup=self._status_for_key)
                    tree.show_root = False
                    tree.guide_depth = 3
                    yield tree
                yield Static("", id="usage")
            yield Static("", id="detail")
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
        self.render_usage()
        self.collect()
        self.scan_usage()
        self.set_interval(REFRESH_S, self.collect)
        self.set_interval(USAGE_SCAN_S, self.scan_usage)

    def on_resize(self) -> None:
        self._apply_detail_visibility()

    def _apply_detail_visibility(self) -> None:
        show = self.detail_forced if self.detail_forced is not None else self.size.height >= SHORT_ROWS
        self.query_one("#detail", Static).set_class(not show, "hidden")
        show_u = self.usage_forced if self.usage_forced is not None else self.size.width >= NARROW_COLS
        usage = self.query_one("#usage", Static)
        usage.set_class(not show_u, "hidden")
        if show_u and usage.size.width and usage.size.width != self._usage_width:
            self.render_usage()

    # ---- data -------------------------------------------------------------
    @work(thread=True, exclusive=True, group="collect")
    def collect(self) -> None:
        try:
            snap = self.collector.snapshot()
        except Exception:        # keep the UI alive; details go to the log, not the screen
            log.exception("snapshot failed")
            self.call_from_thread(self._set_status, "collector error — see logs/monitor.log")
            return
        try:
            self.call_from_thread(self.apply_snapshot, snap)
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
            self.call_from_thread(self.render_usage)

    def render_usage(self) -> None:
        try:
            usage = self.query_one("#usage", Static)
        except NoMatches:
            return
        width = max(30, (usage.size.width or 50) - 2)      # minus padding
        self._usage_width = usage.size.width
        usage.update(usage_text(self.usage, self.usage_metric, width))

    def action_toggle_usage(self) -> None:
        usage = self.query_one("#usage", Static)
        self.usage_forced = usage.has_class("hidden")      # hidden -> show, shown -> hide
        self._apply_detail_visibility()
        if self.usage_forced:
            self.render_usage()

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
    def _status_for_key(self, key: str) -> str:
        obj = self.objects.get(key)
        if isinstance(obj, Instance) and obj.status_detail.startswith("collector error"):
            return "error"
        if isinstance(obj, (Instance, Agent)):
            return obj.status
        if isinstance(obj, ArchiveGroup):
            return "archived"
        return ""

    def _name_width(self, depth: int, tree: Tree | None = None) -> int:
        """Name column so that activity text starts in the same column on every row.
        A head row is: icon(2) + name + space + strip(w). A child row at `depth` is:
        guides(depth*guide_depth) + icon(2) + name, so its name column absorbs the difference."""
        if not self.settings.align_columns:
            return 0
        base = max(8, self.settings.name_column_width)
        if depth == 0:                       # head rows render the strip separately
            return base
        guide = (tree.guide_depth if tree is not None else 3)
        strip = (self._strip_width + 1) if self._strip_width else 0
        return max(8, base + strip - depth * guide)

    @staticmethod
    def _ikey(inst: Instance) -> str:
        return f"i:{inst.pid}"          # pid, not session id: a stale registry entry can share one

    def _upsert_instance(self, tree: Tree, inst: Instance, now: float, width: int,
                         seen: set[str], expand: bool = True) -> TreeNode:
        """Add or refresh one head-agent row and its subagent leaves."""
        key = self._ikey(inst)
        seen.add(key)
        self.objects[key] = inst
        node = self.nodes.get(key)
        label = instance_label(inst, now, self.settings.show_status_text, self._name_width(0),
                               self._strip_width)
        label.truncate(width, overflow="ellipsis")
        if node is None:
            node = tree.root.add(label, data=key, expand=expand)
            self.nodes[key] = node
        else:
            node.set_label(label)
        current = [a for a in inst.agents if not a.archived]
        archived = [a for a in inst.agents if a.archived]
        node.allow_expand = bool(current or archived)

        def upsert_leaf(a: Agent, parent: TreeNode, depth: int) -> None:
            akey = f"{key}/a:{a.agent_id}"
            seen.add(akey)
            self.objects[akey] = a
            alabel = agent_label(a, now, self.settings.show_status_text, self._name_width(depth, tree))
            alabel.truncate(width - 2 * tree.guide_depth, overflow="ellipsis")
            anode = self.nodes.get(akey)
            if anode is not None and anode.parent is not parent:     # moved between live/archived
                try:
                    anode.remove()
                except Exception:
                    pass
                anode = None
            if anode is None:
                self.nodes[akey] = parent.add_leaf(alabel, data=akey)
            else:
                anode.set_label(alabel)

        # 1) current-run subagents sit directly under the head
        for a in current:
            upsert_leaf(a, node, 1)

        # 2) the archived accordion always comes last. If a new live agent was appended
        #    after it, rebuild the group at the end (rare: only when a team grows).
        gkey = f"{key}/archived"
        group = self.nodes.get(gkey)
        if archived:
            if group is not None and node.children and node.children[-1] is not group:
                was_open = group.is_expanded
                for akey in [k for k in self.nodes if k.startswith(f"{key}/a:")]:
                    if self.nodes[akey].parent is group:
                        self.nodes.pop(akey, None)
                try:
                    group.remove()
                except Exception:
                    pass
                group = None
            else:
                was_open = False
            glabel = Text()
            glabel.append(f"archived ({len(archived)})", style="dim")
            glabel.append("  ")
            glabel.append("earlier runs of this session · logs only", style="dim italic")
            glabel.truncate(width - tree.guide_depth, overflow="ellipsis")
            if group is None:
                group = node.add(glabel, data=gkey, expand=was_open)
                self.nodes[gkey] = group
            else:
                group.set_label(glabel)
            seen.add(gkey)
            self.objects[gkey] = ArchiveGroup(inst, archived)
            for a in archived:
                upsert_leaf(a, group, 2)
        # (a group with nothing left in it is dropped by the stale-key sweep)
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
        expanded = {k: n.is_expanded for k, n in self.nodes.items() if "/a:" not in k}
        tree.clear()
        self.nodes.clear()
        self.objects.clear()
        seen: set[str] = set()
        for inst in instances:
            self._upsert_instance(tree, inst, now, width, seen,
                                  expand=expanded.get(self._ikey(inst), True))
            gkey = f"{self._ikey(inst)}/archived"
            if gkey in self.nodes and expanded.get(gkey, False):
                self.nodes[gkey].expand()
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

        recent = errors.recent(600)
        self._set_status(fleet_line(instances, self.ordering, now, self.settings.show_status_text,
                                    recent, self.collector.iterm.available),
                         fleet_tooltip(instances, self.ordering, recent, self.collector.iterm.available))
        self._refresh_detail()

    # ---- detail pane ------------------------------------------------------
    def on_tree_node_highlighted(self, event: Tree.NodeHighlighted) -> None:
        self._refresh_detail(event.node)

    def _refresh_detail(self, node: TreeNode | None = None) -> None:
        try:
            tree = self.query_one("#tree", Tree)
            detail = self.query_one("#detail", Static)
        except NoMatches:
            return
        node = node or tree.cursor_node
        if node is None or node.data is None:
            detail.update(Text("select an instance", style="dim"))
            return
        obj = self.objects.get(node.data)
        detail.update(detail_text(obj, time.time(), self.ordering) if obj else Text(""))

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
        # Labels are rebuilt from the current instances on the main screen's next apply.
        self.call_later(lambda: self.apply_snapshot(list(self.instances)) if self.instances else None)

    def action_toggle_detail(self) -> None:
        detail = self.query_one("#detail", Static)
        self.detail_forced = detail.has_class("hidden")   # hidden -> force show, shown -> force hide
        self._apply_detail_visibility()

    def action_refresh_now(self) -> None:
        self.collector.iterm.refresh(force=True)
        self.collect()

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
        ok, why = self.collector.focus_instance(inst, label)
        if ok:
            self.notify("brought the Claude app forward" if inst.is_app else f"focused {inst.iterm_title or inst.name}",
                        timeout=2)
        else:
            self.notify(f"focus failed: {why[:60]} — see logs/monitor.log", severity="warning", timeout=4)


if __name__ == "__main__":
    import argparse
    import os
    ap = argparse.ArgumentParser(description="Monitor the Claude Code instances on this Mac.")
    ap.add_argument("--order", default=os.environ.get("CLAUDE_MONITOR_ORDER") or None,
                    choices=[o.name for o in ORDERINGS],
                    help="initial instance ordering for this run (default: the saved setting)")
    ap.add_argument("--usage", action="store_true",
                    default=os.environ.get("CLAUDE_MONITOR_USAGE", "") not in ("", "0", "false"),
                    help="start with the usage panel shown (default: the saved setting)")
    args = ap.parse_args()
    app = ClaudeMonitor(order_name=args.order, show_usage=True if args.usage else None)
    try:
        app.run()
    except Exception:            # errors outside Textual's own handling (startup, driver)
        log.exception("monitor crashed")
        raise
    finally:
        log.info("monitor exiting (return code %s)", app.return_code)
    sys.exit(app.return_code or 0)   # so the supervisor sees a crash as a non-zero exit
