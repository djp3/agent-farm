# Claude Monitor

A read-only Textual TUI that shows every Claude Code instance running on this Mac,
what each one is doing, whether it is busy / idle / waiting, and any subagents or
teammates nested under the head agent that spawned them.

Nothing is installed into the Claude instances. The monitor only reads files Claude
Code already writes (`~/.claude/sessions`, the transcripts under `~/.claude/projects`,
the `subagents/` folders and `~/.claude/teams`) and asks iTerm2's bundled `it2` CLI
which tab owns which tty.

## Run

    ./supervisor.sh          # hot-reloads main.py whenever a .py file changes
    .venv/bin/python main.py # plain run
    .venv/bin/python collector.py   # one-shot text dump, handy for debugging

First time: `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt`

## Keys

| key | action |
|-----|--------|
| ↑ ↓ | move |
| enter / space | fold or unfold a head agent's team |
| f | focus that instance's iTerm2 tab, or bring the Claude desktop app forward for a session running inside it |
| d | show / hide the detail pane under the tree (auto-hides below 22 rows) |
| u | show / hide the usage panel right of the tree (hidden at launch; `--usage` starts it shown) |
| m | cycle the usage bars: output tokens, input tokens incl. cache, API messages (not in the footer; the panel's header names it) |
| s | open the full-screen settings panel (esc returns) |
| r | refresh now |
| q | quit |

## Ordering

The default ordering, `attention`, puts first what needs you:

1. **waiting** for your response (permission prompt, question, plan approval), longest wait first
2. **working** on your input, most recent prompt from you first
3. **idle**, most recently gone idle first
4. **inactive**: dead registry entries and unregistered processes

The alternatives are `activity` (most recent transcript event first), `started` (launch
order) and `name`. Choose one in Settings (`s`); it applies immediately and at the next launch.
`--order NAME` or the `CLAUDE_MONITOR_ORDER` environment variable override it for one run.

Orderings live in `ordering.py`. An `Ordering` is a tuple of `Tier`s, each with a `match`
predicate and a within-tier sort `key`; an instance lands in the first tier that matches.
To add one, define it and append it to `ORDERINGS`. The tree only rebuilds when the visible
order actually changes, and it keeps the cursor on the same instance and each row's fold state.

## Usage panel

Hidden by default; press `u` to show or hide it, or launch with `--usage` (or
`CLAUDE_MONITOR_USAGE=1`) to start with it open.
One bar per local day for the last 14 days, labelled by weekday, aggregated from every
transcript under `~/.claude/projects` (head sessions and subagents). The measure is named
in the title and as a header over the value column; `m` cycles output tokens, input tokens
including cache, and API messages. Bars are the busy green, dimmed, with today at full
green; values are dimmed except today's and the window maximum. Beneath the days, a
full-width stacked bar shows output tokens by model (top three plus Other) with a legend;
each model keeps its color and glyph texture for the life of the app, and the textures
mean the stack reads without color. Claude Code writes one row per content block of an API
message, each repeating the same usage object, so usage is counted once per message id.
The first scan of all transcripts takes about a second; afterwards only new bytes are read
every 10 s. No prices are hard-coded: the "logged cost" line sums Claude Code's own
`cost-state` checkpoints, which are written at session end, so today's sessions are not in
it yet. Plan rate-limit windows (the `/usage` command) are fetched from the API by Claude
Code and are not cached locally, so they are not shown.

## The fleet line

The single line above the tree is the whole fleet at a glance, in the same glyph and color
system as the rows: one `■` per head session, grouped and colored waiting → busy → idle →
inactive, so the count of each color is the count of sessions in that state; then the
fleet-wide subagent tally in circles and asterisks. Hover the line for a plain-text
explanation of every glyph and the current counts. Two red notes appear only when something
is wrong: `it2 missing` if iTerm2's CLI cannot be found (the f key needs it), and `⚠ N` for
warnings logged in the last ten minutes (it clears when things have been quiet; the tooltip
keeps the total since launch). With **Show status text** on, the counts are also spelled out in words.

## Reading a row

Every row starts with exactly one glyph, so names line up:

| glyph | meaning |
|-------|---------|
| ▶ / ▼ | the row has children to fold or unfold (filled: collapsed / expanded) |
| ▷ | nothing beneath this row |

Sessions running inside the Claude desktop app rather than a terminal carry a `⧉` after
their name, and their detail pane has a `where` line. The glyph carries the row's state as a color. The four head-session colors are the ones
iTerm2 paints on its own Claude status dot, so the tab bar and the monitor agree:

| color | head session | subagent |
|-------|--------------|----------|
| orange | **waiting** for you (permission prompt, question, plan approval) | |
| green | **busy** on your input | **running**; dimmed while **starting** |
| blue | **idle**, ready for your next instruction | dimmed: **dormant**, finished but resident |
| grey | **inactive**: dead registry entry or unregistered process | dimmed: **archived**, log only |
| magenta | | **stale**: silent too long mid tool-call, likely killed |
| red | the collector failed on this row (see `logs/monitor.log`) | |

After the glyph comes the session name, then the **agent strip**: one circle per current-run
subagent and one asterisk `✱` per five, colored by the same palette and ordered running →
starting → stale → dormant. The name comes first so subagent rows, indented by the tree
guides, hang visibly under their lead's name. `●` filled means working, `○` hollow means finished and dormant, `◌` is
still starting, `◍` is stale. Shape carries the state, so it reads without color. Archived
agents are not in the strip; their accordion row gives the count. The strip column is
exactly as wide as the widest strip currently on screen, so it costs no room when teams
are small, and strips sit in the same column on every row, so teams compare at a glance.
With **Align activity text** on
(the default) names are padded to a fixed column so the activity text lines up too.

The state word and its age ("idle for 45s"), the `cwd`, and the `git` repository (repo @
branch, origin remote) are the first lines of the detail pane. When the session runs in a linked git
worktree, such as one made by `claude --worktree`, the git line says "worktree of <main
repo>" (or "Claude worktree of …" when it lives under `.claude/worktrees`).

## Settings

`s` opens a full-screen settings panel; esc returns. Changes apply at once and are saved to
`monitor_settings.json` next to the code (override the path with `CLAUDE_MONITOR_SETTINGS`).

- **Show status text in rows** (off by default): puts the state word and age back into every
  row for readers who cannot rely on the triangle color.
- **Usage panel open at launch** (off by default).
- **Align activity text** (on by default): pad names to a fixed column, 26 characters unless
  `name_column_width` is changed in the JSON file, clipping longer names with an ellipsis.
- **Ordering** (attention by default): the only place the ordering is changed.

`--order` and `--usage` on the command line override the saved defaults for one run without
changing the file. Settings live in `settings.py`; a new one is a field with a default.

## Status meanings

- **busy / idle** come straight from Claude Code's own registry entry.
- **waiting** is inferred from the transcript: a PermissionRequest hook fired and has
  not been followed by a tool result, or an AskUserQuestion / ExitPlanMode call is open.
- Subagents are **running** while their transcript changed in the last 90 s, or ends mid
  tool-call and is under 15 min old; **dormant** once the head agent receives their final
  teammate message, or the transcript ends with a message and goes quiet (they stay resident
  in the head process and can be messaged again); **stale** when it ends mid tool-call and
  has been silent over 15 min (killed or crashed); **archived** when their last activity
  predates the current head process, or the head process is gone.
- A head agent's row re-opens on its own when one of its current-run subagents has a
  material update: a new agent appears, an agent changes state, or an agent posts a new
  message. Plain tool calls do not count, so a row you folded during a long subagent run
  stays folded until something worth seeing happens.
- Archived subagents sit under a collapsed **archived (N)** row beneath their head agent.
  Teammates live inside the head process, so agents from an earlier run of a resumed session
  are transcripts only and cannot be woken; the accordion keeps them browsable without
  crowding the live list. Enter or space on the row unfolds it; the monitor never opens it
  by itself.

## Logs

Errors never show in detail on screen; the fleet line only gets a red `⚠ N` badge for the
last ten minutes' warnings. A single slow reply from iTerm2's `it2` is logged at INFO and
keeps the previous tab map; only three failures in a row become a warning.
Everything goes to `logs/` next to the code:

| file | what |
|------|------|
| `logs/monitor.log` | the app and collector: it2 calls (focus attempts and why they failed), unreadable registry files, malformed transcript rows, per-instance collector failures, and any crash traceback |
| `logs/supervisor.log` | supervisor events: starts, file-change restarts, child exit codes |

The child's stderr is deliberately *not* captured: Textual draws the UI on stderr.
Crashes inside the UI are logged by `main.py` itself and exit with code 1.

Noisy per-tick failures are logged at most once a minute per source.

## License

GPL-3.0-or-later. See [LICENSE](LICENSE).
