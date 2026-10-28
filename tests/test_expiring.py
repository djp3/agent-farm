"""The expiring pane: the sessions Claude Code's cleanup deletes next, and keep, the one
write agent-farm makes. Claude Code judges a session by modification times, so keep sets
them to now on everything it judges by and changes no file's contents."""
import asyncio
import json
import logging
import os
import time
from datetime import date, datetime
from pathlib import Path

import main
from expiring import DAY, KEPT_SHOW_S, ExpiryScanner, read_period
from settings import Settings

SECRET = "sk-ant-SECRET-NEVER-LOGGED"
# Noon today: days and "expires today" never straddle midnight, whenever the tests run.
NOON = datetime.combine(date.today(), datetime.min.time()).timestamp() + 12 * 3600


def _transcript(root: Path, sid: str, age_days: float, *, entry="cli", cwd="/Users/x/code/proj",
                title=None, project="-Users-x-code-proj", now=None) -> Path:
    now = time.time() if now is None else now
    p = root / "projects" / project / f"{sid}.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    rows = [{"type": "user", "entrypoint": entry, "cwd": cwd, "sessionId": sid,
             "message": {"role": "user", "content": "hello"}}]
    if title:
        rows.append({"type": "ai-title", "aiTitle": "a name Claude Code gave it"})
        rows.append({"type": "custom-title", "customTitle": title})
    p.write_text("".join(json.dumps(r) + "\n" for r in rows))
    t = now - age_days * DAY
    os.utime(p, (t, t))
    return p


def _claude(tmp_path: Path, period=None) -> Path:
    root = tmp_path / ".claude"
    root.mkdir(parents=True)
    settings = {"env": {"ANTHROPIC_API_KEY": SECRET}}
    if period is not None:
        settings["cleanupPeriodDays"] = period
    (root / "settings.json").write_text(json.dumps(settings))
    return root


def test_it_lists_what_turns_30_days_old_within_two_weeks_soonest_first(tmp_path):
    now = NOON
    root = _claude(tmp_path)
    _transcript(root, "aaaa1111-due", 30.5, title="past due", now=now)
    _transcript(root, "bbbb2222-soon", 29.2, title="tomorrow", now=now)
    _transcript(root, "cccc3333-later", 20, title="in ten days", now=now)
    _transcript(root, "dddd4444-fresh", 10, title="not yet", now=now)
    _transcript(root, "eeee5555-desk", 29, entry="claude-desktop", title="from the app", now=now)
    released = _transcript(root, "ffff6666-rel", 25, entry="local-agent", title="released", now=now)
    released.with_name("ffff6666-rel.desktop-released.json").write_text('{"reason":"delete"}')
    folder = root / "projects" / "-Users-x-code-proj" / "bbbb2222-soon" / "subagents"
    folder.mkdir(parents=True)
    (folder / "agent-1.jsonl").write_text("x" * 5000)

    sc = ExpiryScanner(root)
    assert sc.scan(now) is True and sc.period_days == 30
    got = sc.entries(now)
    assert [e.title for e in got] == ["past due", "tomorrow", "released", "in ten days"]
    assert [e.soon(now) for e in got] == [True, True, False, False]
    soon = got[1]
    assert soon.cwd == "/Users/x/code/proj" and abs(soon.deletes_at - (now + 0.8 * DAY)) < 2
    assert soon.size == soon.path.stat().st_size + 5000            # its subagents count too
    assert sc.scan(now) is False                                     # nothing moved


def test_the_period_comes_from_claude_codes_settings_and_0_means_never(tmp_path, caplog):
    now = NOON
    root = _claude(tmp_path, period=10)
    _transcript(root, "aaaa1111", 5, title="five days", now=now)
    sc = ExpiryScanner(root)
    sc.scan(now)
    assert sc.period_days == 10 and [e.title for e in sc.entries(now)] == ["five days"]
    (root / "settings.json").write_text(json.dumps({"cleanupPeriodDays": 0}))
    sc.scan(now)
    assert sc.period_days is None and sc.entries(now) == []
    text = main.expiring_text([], now, 90, 10, None).plain
    assert "keeps every session here (cleanupPeriodDays is 0)" in text
    # unreadable settings: the default, and nothing from the file in the log
    (root / "settings.json").write_text('{"env": {"KEY": "' + SECRET + '"}, oops')
    with caplog.at_level(logging.WARNING, logger="agent_farm.expiring"):
        assert read_period(root) == 30
    assert caplog.records and all(SECRET not in r.getMessage() for r in caplog.records)


def test_the_name_is_the_one_resume_shows(tmp_path):
    now = NOON
    root = _claude(tmp_path)
    p = _transcript(root, "aaaa1111", 25, title="from the rows", now=now)
    sc = ExpiryScanner(root)
    sc.scan(now)
    assert sc.entries(now)[0].title == "from the rows"
    named = p.with_suffix("")
    named.mkdir()
    (named / "custom-title.json").write_text(json.dumps({"customTitle": "renamed\x1b[31m it"}))
    os.utime(p, (now - 25 * DAY + 1, now - 25 * DAY + 1))           # a changed file is read again
    sc.scan(now)
    assert sc.entries(now)[0].title == "renamed[31m it"             # no escape reaches the screen


def test_keep_restarts_every_clock_claude_code_judges_by_and_writes_nothing(tmp_path, caplog):
    now = NOON
    root = _claude(tmp_path)
    sid = "abcd1234-5678-90ab-cdef-000000000001"
    p = _transcript(root, sid, 29.5, title="worth keeping", now=now)
    old = now - 29.5 * DAY
    project = p.parent
    folder = project / sid
    outside = tmp_path / "outside.txt"
    outside.write_text("not Claude Code's")
    files = {
        project / f"{sid}.precompact.json": "{}",
        folder / "custom-title.json": '{"customTitle": "worth keeping"}',
        folder / "tool-results" / "a.txt": "result a",
        folder / "tool-results" / "nested" / "b.txt": "result b",
        folder / "subagents" / "agent-1.jsonl": "{}\n",
    }
    for f, body in files.items():
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(body)
    (folder / "tool-results" / "link.txt").symlink_to(outside)
    sides = [root / "file-history" / sid, root / "session-env" / sid, root / "tasks" / f"session-{sid[:8]}"]
    for d in sides:
        d.mkdir(parents=True)
    for f in [*files, outside, *sides]:
        os.utime(f, (old, old))
    before = {f: f.read_bytes() for f in [p, *files]}

    sc = ExpiryScanner(root)
    sc.scan(now)
    entry = sc.entries(now)[0]
    with caplog.at_level(logging.INFO, logger="agent_farm.expiring"):
        n = sc.keep(entry, now)
    touched = [p, *[f for f in files if "subagents" not in str(f)], *sides]
    assert n == len(touched)
    assert all(abs(f.stat().st_mtime - now) < 1 for f in touched)
    assert abs((folder / "subagents" / "agent-1.jsonl").stat().st_mtime - old) < 1   # kept with the transcript
    assert abs(outside.stat().st_mtime - old) < 1                                      # never through a symlink
    assert all(f.read_bytes() == body for f, body in before.items())
    assert any("kept session abcd1234 for another 30 days" in r.getMessage() for r in caplog.records)
    # it leaves the window, but stays listed, green, for a minute
    sc.scan(now)
    shown = sc.entries(now)
    assert [e.session_id for e in shown] == [sid] and shown[0].kept_at == now
    assert sc.entries(now + KEPT_SHOW_S + 1) == []


def test_the_pane_text_fits_its_height_and_says_how_many_more(tmp_path):
    now = NOON
    root = _claude(tmp_path)
    for k, age in enumerate([29.9, 29.5, 28, 27, 26, 25, 18, 17]):
        _transcript(root, f"{k:04d}aaaa", age, title=f"session {k}", now=now)
    sc = ExpiryScanner(root)
    sc.scan(now)
    entries = sc.entries(now)
    lines = main.BOTTOM_ROWS - 1
    text = main.expiring_text(entries, now, 96, lines, 30, cursor=1)
    plain = text.plain.rstrip("\n").split("\n")
    assert len(plain) == lines
    assert plain[0].startswith("expiring  Claude Code deletes a session 30 days after its last use")
    assert "session 0" in plain[5] and "expires today" in plain[5] and plain[5].endswith(" keep ")
    assert plain[-1].strip() == "+ 2 more this week, 2 next week  ·  scroll for them"
    assert main.expiry_room(lines, len(entries)) == 4
    # the first two need you: a yellow icon and white name; the rest are gray
    spans = {text.plain[s.start:s.end].strip(): str(s.style) for s in text.spans}
    assert spans["session 0"] == f"bold {main.WHITE}" and spans["session 3"] == f"bold {main.GRAY}"
    # the mouse finds a row and its keep button by their meta
    metas = [s.style.meta for s in text.spans if not isinstance(s.style, str) and s.style.meta]
    assert {"row": 1} in metas and {"keep": 1} in metas
    # a scrolled pane says how many are above
    scrolled = main.expiring_text(entries, now, 96, lines, 30, top=2).plain.rstrip("\n").split("\n")
    assert scrolled[-1].strip().startswith("+ 2 above, ")


def test_a_kept_session_reads_green_and_the_chart_counts_by_day(tmp_path):
    now = NOON
    root = _claude(tmp_path)
    for k in range(5):
        _transcript(root, f"{k:04d}bbbb", 25.0 + k * 0.01, title=f"same day {k}", now=now)
    sc = ExpiryScanner(root)
    sc.scan(now)
    entries = sc.entries(now)
    text = main.expiring_text(entries, now, 96, 30, 30).plain
    assert " 5 " in text.split("\n")[1]                  # five on one day: taller than the chart
    sc.keep(entries[0], now)
    text = main.expiring_text(sc.entries(now), now, 96, 30, 30).plain
    assert f"✓ {entries[0].title}" in text and "kept · safe until" in text
    assert " 4 " in text.split("\n")[1]                  # the kept one no longer counts


def _lit_blocks(text):
    """(chart row from the top, day) of each block drawn in the highlight."""
    lines = text.plain.split("\n")
    starts = [sum(len(x) + 1 for x in lines[:k]) for k in range(len(lines))]
    lit = []
    for s in text.spans:
        if str(s.style).endswith(main.WHITE) and text.plain[s.start:s.end].strip() and s.start < starts[4]:
            row = max(k for k in range(4) if starts[k] <= s.start)
            lit.append((row, (s.start - starts[row] - 2) // 3))
    return lit


def test_the_selected_session_lights_its_own_block_in_its_days_bar(tmp_path):
    now = NOON
    root = _claude(tmp_path)
    # day +3: two sessions, stacked soonest at the bottom; day +5: one; day +6: five
    for sid, days, minutes in [("a", 3, 0), ("b", 3, 10), ("c", 5, 0)] + [(f"d{k}", 6, k) for k in range(5)]:
        age = 30 - days - minutes / 1440
        _transcript(root, f"{sid}-0000", age, title=sid, now=now)
    sc = ExpiryScanner(root)
    sc.scan(now)
    entries = sc.entries(now)
    assert [e.title for e in entries] == ["a", "b", "c", "d0", "d1", "d2", "d3", "d4"]
    chart = lambda cursor: _lit_blocks(main.expiring_text(entries, now, 96, 30, 30, cursor=cursor))
    assert chart(None) == []                      # nothing lit while the pane has no focus
    assert chart(0) == [(3, 3)]                   # a: the bottom block of day 3 (chart rows are 1-3)
    assert chart(1) == [(2, 3)]                   # b: the block above it
    assert chart(2) == [(3, 5)]
    assert chart(4) == [(2, 6)]                   # d1: second from the bottom
    assert chart(6) == [(1, 6)]                   # d3: above the chart, so the count lights up
    sc.keep(entries[0], now)                      # a kept session leaves the chart and lights nothing
    kept = sc.entries(now)
    assert _lit_blocks(main.expiring_text(kept, now, 96, 30, 30, cursor=0)) == []
    assert _lit_blocks(main.expiring_text(kept, now, 96, 30, 30, cursor=1)) == [(3, 3)]   # b is alone now


def _app_test(tmp_path, size, settings, steps):
    root = _claude(tmp_path)
    now = time.time()
    _transcript(root, "aaaa1111-due", 29.5, title="due tomorrow", now=now)
    _transcript(root, "bbbb2222-later", 22, title="next week", now=now)

    async def go():
        app = main.AgentFarm(show_usage=True, settings=settings)
        app.collector.snapshot = lambda: []
        app.expiry = ExpiryScanner(root)
        async with app.run_test(size=size) as pilot:
            await pilot.pause(1.5)
            await steps(app, pilot, root)
            await pilot.press("q")

    asyncio.run(go())


def test_two_columns_with_the_expiring_pane_as_tall_as_the_farm_chart(tmp_path):
    async def steps(app, pilot, root):
        left = app.query_one("#left")
        assert [w.id for w in left.children] == ["fleet", "tree", "detail", "expiring"]
        pane, farm = app.query_one("#expiring"), app.query_one("#farm")
        assert pane.outer_size.height == farm.outer_size.height == main.BOTTOM_ROWS
        assert pane.region.y == farm.region.y                          # the lines above them meet
        assert app.query_one("#usage").region.height == left.region.height
        assert [e.title for e in pane.entries] == ["due tomorrow", "next week"]
        # tab to the pane: the detail pane follows its selected session, and k keeps it
        await pilot.press("tab")
        await pilot.pause(0.2)
        detail = str(app.query_one("#detail").render())
        assert detail.startswith("due tomorrow") and "claude --resume aaaa1111-due" in detail
        await pilot.press("k")
        await pilot.pause(0.5)
        p = next((root / "projects").glob("*/aaaa1111-due.jsonl"))
        assert time.time() - p.stat().st_mtime < 5
        assert pane.entries[0].kept_at and "kept · safe until" in pane.render().plain
        await pilot.press("tab")                                        # back to the tree
        await pilot.pause(0.2)
        assert not str(app.query_one("#detail").render()).startswith("due tomorrow")

    _app_test(tmp_path, (140, 46), Settings(), steps)


def test_the_setting_hides_the_pane_and_a_short_window_does_too(tmp_path):
    async def steps(app, pilot, root):
        assert not app.query_one("#expiring").display
        assert [w.id for w in app.query_one("#left").children if w.display] == ["fleet", "tree", "detail"]

    _app_test(tmp_path, (140, 46), Settings(show_expiring=False), steps)

    async def short(app, pilot, root):
        assert not app.query_one("#expiring").display and app.query_one("#detail").display

    _app_test(tmp_path / "short", (140, main.SHORT_ROWS + main.BOTTOM_ROWS - 1), Settings(), short)
