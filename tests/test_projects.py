"""The project graph splits the week's percent across git repos. A message counts toward the
folder it ran in, worktrees and clones merge, the rows add up to the week, and the two graphs
turn on and off on their own with the panel closing when both are off."""
import asyncio
import json
import re
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

import main
import usage
from expiring import ExpiryScanner
from limits import LimitsReport, Window
from projects import Projects, project_label
from settings import Settings
from usage import DayUsage, UsageScanner, message_weight


def _repo(folder: Path, origin: str = "") -> Path:
    (folder / ".git").mkdir(parents=True)
    remote = f'[remote "origin"]\n\turl = {origin}\n\tfetch = +refs/heads/*:refs/remotes/origin/*\n' if origin else ""
    (folder / ".git" / "config").write_text('[core]\n\tbare = false\n' + remote + '[branch "main"]\n\tremote = origin\n')
    return folder


def test_every_folder_in_a_repo_is_that_repo_and_each_repo_is_its_own_project(tmp_path):
    billing = _repo(tmp_path / "git" / "billing-checkout", "git@github.com:acme/app_billing.git")
    engine = _repo(tmp_path / "engine", "git@github.com:acme/app_shipper.git")
    (billing / "src" / "deep").mkdir(parents=True)
    p = Projects()
    assert p.key(str(billing)) == p.key(str(billing / "src" / "deep")) == "acme/app_billing"
    assert p.key(str(engine)) == "acme/app_shipper"             # the same org is still two projects
    assert project_label("acme/app_billing") == "app_billing"


def test_a_worktree_counts_toward_its_repo(tmp_path):
    repo = _repo(tmp_path / "repo", "git@github.com:acme/app.git")
    git_dir = repo / ".git" / "worktrees" / "feature"
    git_dir.mkdir(parents=True)
    (git_dir / "commondir").write_text("../..\n")
    tree = tmp_path / "elsewhere" / "feature"
    tree.mkdir(parents=True)
    (tree / ".git").write_text(f"gitdir: {git_dir}\n")
    assert Projects().key(str(tree)) == "acme/app"
    # the same without a remote: the repo's own folder
    (repo / ".git" / "config").write_text("[core]\n")
    assert Projects().key(str(tree)) == str(repo)


def test_a_submodule_is_a_repo_of_its_own(tmp_path):
    repo = _repo(tmp_path / "repo", "git@github.com:acme/app.git")
    git_dir = repo / ".git" / "modules" / "lib"
    git_dir.mkdir(parents=True)
    (git_dir / "config").write_text('[remote "origin"]\n\turl = https://github.com/acme/lib\n')
    lib = repo / "lib"
    lib.mkdir()
    (lib / ".git").write_text("gitdir: ../.git/modules/lib\n")
    assert Projects().key(str(lib)) == "acme/lib"


def test_clones_merge_and_a_token_in_the_url_goes_no_further(tmp_path):
    a = _repo(tmp_path / "a", "git@github.com:acme/app.git")
    b = _repo(tmp_path / "b", "https://ghp_SECRET@github.com/acme/app.git")
    p = Projects()
    assert p.key(str(a)) == p.key(str(b)) == "acme/app"


def test_a_folder_outside_any_repo_counts_toward_where_its_session_started(tmp_path):
    talk = tmp_path / "talk"
    (talk / "examples" / "texts").mkdir(parents=True)
    other = tmp_path / "other"
    other.mkdir()
    p = Projects()
    assert p.key(str(talk / "examples" / "texts"), str(talk)) == str(talk)
    assert p.key(str(other), str(talk)) == str(other)
    assert p.key(str(other)) == str(other)
    assert project_label(str(talk)) == "talk"
    assert project_label(str(Path.home())) == "~"
    # a session started above a repo: the repo still wins inside it
    inner = _repo(talk / "code", "git@github.com:acme/code.git")
    assert p.key(str(inner), str(talk)) == "acme/code"


def test_a_message_is_worth_what_its_model_and_token_kinds_are_worth():
    assert message_weight("fable-5-1", {"output_tokens": 1}) == 50
    assert message_weight("opus-5-5", {"output_tokens": 1}) == 20
    assert message_weight("opus-4-8[1m]", {"output_tokens": 1}) == 25
    assert message_weight("haiku-5-5", {"output_tokens": 1}) < message_weight("haiku-4-5-20251001", {"output_tokens": 1})
    assert message_weight("fable-5-1", {"cache_read_input_tokens": 4}) == 1      # a quarter of Fable 5's
    assert message_weight("fable-5", {"cache_read_input_tokens": 4}) == 4
    assert message_weight("something-new", {"output_tokens": 1}) == 25
    five = {"cache_creation_input_tokens": 8, "cache_creation": {"ephemeral_5m_input_tokens": 8}}
    hour = {"cache_creation_input_tokens": 8, "cache_creation": {"ephemeral_1h_input_tokens": 8}}
    assert message_weight("fable-5-1", five) == 8 * 12.5
    assert message_weight("fable-5-1", hour) == 8 * 20
    assert message_weight("fable-5-1", {"cache_creation_input_tokens": 8, "cache_creation": "odd"}) == 8 * 12.5


def _assistant(mid, cwd, at, model="claude-fable-5-1", out=100, session="s1"):
    return json.dumps({"type": "assistant", "timestamp": datetime.fromtimestamp(at, timezone.utc).isoformat(),
                       "cwd": cwd, "sessionId": session,
                       "message": {"id": mid, "model": model, "usage": {"output_tokens": out}}}) + "\n"


def _scanner(tmp_path, monkeypatch, rows_by_file) -> UsageScanner:
    root = tmp_path / "claude-projects"
    monkeypatch.setattr(usage, "PROJECTS_DIR", root)
    for name, rows in rows_by_file.items():
        f = root / "slug" / name
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("".join(rows))
    u = UsageScanner()
    u.scan()
    return u


def test_agents_in_one_repo_add_up_and_one_that_moves_is_split(tmp_path, monkeypatch):
    a = _repo(tmp_path / "a", "git@github.com:acme/a.git")
    b = _repo(tmp_path / "b", "git@github.com:acme/b.git")
    now = time.time()
    u = _scanner(tmp_path, monkeypatch, {
        "one.jsonl": [_assistant("m1", str(a), now), _assistant("m1", str(a), now),      # one message, two blocks
                      _assistant("m2", str(b), now)],
        "two.jsonl": [_assistant("m3", str(a / "src"), now, session="s2")],
    })
    split = u.project_split(now - 3600)
    assert split["acme/a"]["fable-5-1"] == 2 * 100 * 50
    assert split["acme/b"]["fable-5-1"] == 100 * 50
    assert u.project_split(now + 7200) == {}                    # nothing that recent


def test_the_week_is_measured_from_its_start_to_the_hour(tmp_path, monkeypatch):
    a = _repo(tmp_path / "a", "git@github.com:acme/a.git")
    start = (int(time.time() // 3600) - 48) * 3600
    u = _scanner(tmp_path, monkeypatch, {"one.jsonl": [_assistant("before", str(a), start - 60),
                                                       _assistant("after", str(a), start + 60, out=1)]})
    assert u.project_split(start)["acme/a"]["fable-5-1"] == 50


def test_the_graph_reads_a_snapshot_not_the_scanners_tally(tmp_path, monkeypatch):
    a = _repo(tmp_path / "a", "git@github.com:acme/a.git")
    now = time.time()
    u = _scanner(tmp_path, monkeypatch, {"one.jsonl": [_assistant("m1", str(a), now)]})
    before = u.project_split(now - 3600)["acme/a"]["fable-5-1"]
    u._hours[int(now // 3600)][("acme/a", "fable-5-1")] += 999
    assert u.project_split(now - 3600)["acme/a"]["fable-5-1"] == before


NOW = datetime(2026, 11, 4, 7, 0, tzinfo=timezone.utc).timestamp()
RESET = datetime(2026, 11, 7, 7, 0, tzinfo=timezone.utc)
WEEK_34 = Window("weekly_all:", "week", 34.0, RESET, 7 * 86400)


class FakeScanner:
    scanned_once = True

    def __init__(self, split, models=(("fable-5-1", 90), ("opus-5-5", 10))):
        self.split, self.models = split, list(models)
        self.since = None

    def project_split(self, since):
        self.since = since
        return self.split

    def model_split(self, days):
        return self.models


def _values(text):
    return [float(m) for m in re.findall(r"(\d+\.\d)%$", text, re.M)]


def _report(surfaces=(("claude code", 98.0), ("chats", 1.0), ("cowork", 1.0))):
    return LimitsReport((WEEK_34,), "ultra", NOW, "", tuple(surfaces))


def test_the_rows_add_up_to_the_week_with_chats_and_cowork_on_their_own_lines(monkeypatch):
    monkeypatch.setattr(main, "_model_slot", {})
    scan = FakeScanner({"acme/app_billing": Counter({"fable-5-1": 800.0}), "acme/app_shipper": Counter({"fable-5-1": 200.0})})
    plain = main.projects_text(scan, _report(), True, 48, now=NOW).plain
    lines = plain.splitlines()
    assert lines[0] == "Projects · week 34% · estimated"
    assert [line.split()[0] for line in lines[1:]] == ["app_billing", "app_shipper", "chats", "cowork"]
    assert _values(plain) == [26.7, 6.7, 0.3, 0.3]             # 33.32 for Claude Code, 0.34 each for the others
    assert abs(sum(_values(plain)) - 34) <= 0.1
    assert scan.since == RESET.timestamp() - 7 * 86400         # the limit's own week
    assert "$" not in plain
    assert all(len(line) <= 48 for line in lines)


def test_a_surface_at_zero_or_a_reply_without_surfaces_leaves_it_all_to_the_projects(monkeypatch):
    monkeypatch.setattr(main, "_model_slot", {})
    scan = FakeScanner({"acme/a": Counter({"fable-5-1": 1.0})})
    assert _values(main.projects_text(scan, _report(()), True, 48, now=NOW).plain) == [34.0]
    plain = main.projects_text(scan, _report((("claude code", 100.0),)), True, 48, now=NOW).plain
    assert _values(plain) == [34.0] and "chats" not in plain


def test_small_projects_fold_into_one_row_but_never_just_one_of_them(monkeypatch):
    monkeypatch.setattr(main, "_model_slot", {})
    many = {f"acme/p{i}": Counter({"fable-5-1": 100.0 - i}) for i in range(9)}
    plain = main.projects_text(FakeScanner(many), _report(), True, 48, now=NOW).plain
    assert "4 more projects" in plain and "p4" in plain and "p5" not in plain
    assert abs(sum(_values(plain)) - 34) <= 0.2
    six = {k: v for k, v in list(many.items())[:6]}
    plain = main.projects_text(FakeScanner(six), _report(), True, 48, now=NOW).plain
    assert "more projects" not in plain and "p5" in plain


def test_without_a_current_week_it_shows_percents_of_the_last_7_days(monkeypatch):
    monkeypatch.setattr(main, "_model_slot", {})
    scan = FakeScanner({"acme/a": Counter({"fable-5-1": 3.0}), "acme/b": Counter({"fable-5-1": 1.0})})
    off = main.projects_text(scan, _report(), False, 48, now=NOW).plain
    assert off.splitlines()[0] == "Projects · last 7 days · % of use"
    assert _values(off) == [75.0, 25.0] and "chats" not in off
    assert scan.since == NOW - 7 * 86400
    after_reset = main.projects_text(scan, _report(), True, 48, now=RESET.timestamp() + 60).plain
    assert after_reset.startswith("Projects · last 7 days")
    assert main.projects_text(scan, LimitsReport(), True, 48, now=NOW).plain.startswith("Projects · last 7 days")


def test_nothing_used_says_so_and_long_or_odd_names_stay_in_the_panel(monkeypatch):
    monkeypatch.setattr(main, "_model_slot", {})
    assert "nothing used" in main.projects_text(FakeScanner({}), _report(()), True, 48, now=NOW).plain
    zero = LimitsReport((Window("k", "week", 0.0, RESET, 7 * 86400),), "", NOW)
    assert "nothing used" in main.projects_text(FakeScanner({"acme/a": Counter({"x": 1.0})}), zero, True, 48, now=NOW).plain
    scan = FakeScanner({"/tmp/Mitch McConnell steps\x07 down\nfrom the Senate": Counter({"fable-5-1": 1.0}),
                        "acme/tiny": Counter({"fable-5-1": 1e-6})})
    plain = main.projects_text(scan, _report(()), True, 48, now=NOW).plain
    assert "\x07" not in plain
    lines = plain.splitlines()
    assert len(lines) == 3 and lines[1].startswith("Mitch McConnell…") and lines[2].endswith("<0.1%")
    assert all(len(line) <= 48 for line in lines)
    assert not main.UsageScanner().scanned_once
    assert "scanning" in main.projects_text(main.UsageScanner(), None, False, 48).plain


def test_bars_take_the_output_by_model_colors_without_disturbing_them(monkeypatch):
    monkeypatch.setattr(main, "_model_slot", {})
    models = [("fable-5-1", 90), ("opus-5-5", 8), ("fable-5", 5), ("haiku-5-5", 1)]
    split = {"acme/a": Counter({"opus-5-5": 1.0, "fable-5-1": 3.0, "haiku-5-5": 0.5})}
    text = main.projects_text(FakeScanner(split, models), _report(()), True, 48, now=NOW)
    styles = [s.style for s in text.spans]
    blue, orange = main.MODEL_SLOTS[0][0], main.MODEL_SLOTS[1][0]
    assert styles.index(blue) < styles.index(orange) < styles.index(main.MODEL_OTHER[0])     # the bar's order
    assert list(main._model_slot) == ["fable-5-1", "opus-5-5", "fable-5"]                    # what the bar names
    assert main.model_split_text(models, 48) and list(main._model_slot) == ["fable-5-1", "opus-5-5", "fable-5"]


def test_stacked_cells_fill_their_width_and_nothing_vanishes():
    hue = main.MODEL_SLOTS[0][0]
    assert main._stack_cells([(hue, "█", 20.0)], 20).plain == "█" * 20
    assert main._stack_cells([(hue, "█", 20.4)], 20).cell_len == 20
    assert main._stack_cells([(hue, "█", 0.001)], 10).plain == "▏" + " " * 9
    assert main._stack_cells([(hue, "█", 2.5)], 10).plain == "██▌" + " " * 7
    assert main._stack_cells([(hue, "█", 2.0), ("#111111", "▓", 1.5)], 10).plain == "██▓▌" + " " * 6
    assert main._stack_cells([(hue, "█", 2.0), ("#111111", "▓", 0.01)], 10).plain == "██" + " " * 8
    assert main._stack_cells([], 4).plain == " " * 4


def _run_app(tmp_path, monkeypatch, settings, steps, size=(140, 46)):
    a = _repo(tmp_path / "a", "git@github.com:acme/a.git")
    root = tmp_path / "claude"
    (root / "projects").mkdir(parents=True)
    now = time.time()
    monkeypatch.setattr(usage, "PROJECTS_DIR", tmp_path / "claude-projects")
    f = tmp_path / "claude-projects" / "slug" / "one.jsonl"
    f.parent.mkdir(parents=True)
    f.write_text(_assistant("m1", str(a), now))

    async def go():
        app = main.AgentFarm(settings=settings)
        app.collector.snapshot = lambda: []
        app.expiry = ExpiryScanner(root)
        app.limits.refresh = lambda force=False: False
        async with app.run_test(size=size) as pilot:
            await pilot.pause(1.0)
            await steps(app, pilot)
            await pilot.press("q")
    asyncio.run(go())


def _shown(app, selector):
    return app.query_one(selector).display


def test_u_and_p_turn_their_graphs_on_and_off_and_the_panel_closes_with_both_off(tmp_path, monkeypatch):
    async def steps(app, pilot):
        assert not _shown(app, "#usage")                              # both off at launch
        await pilot.press("p")
        await pilot.pause(0.2)
        assert _shown(app, "#usage") and _shown(app, "#projects") and not _shown(app, "#usage-scroll")
        usage_col, projects, farm = app.query_one("#usage"), app.query_one("#projects"), app.query_one("#farm")
        assert projects.region.y == usage_col.region.y                # at the top, the farm stays at the bottom
        assert farm.region.bottom == usage_col.region.bottom
        drawn = str(projects.render()).splitlines()
        assert drawn[0].startswith("Projects") and drawn[1].startswith("a ")
        await pilot.press("u")
        await pilot.pause(0.2)
        assert _shown(app, "#usage-scroll") and _shown(app, "#projects")
        assert app.query_one("#usage-scroll").region.bottom <= projects.region.y     # the project graph is below
        assert "Usage" in str(app.query_one("#usage-body").render())
        await pilot.press("p")
        await pilot.pause(0.2)
        assert _shown(app, "#usage-scroll") and not _shown(app, "#projects")
        await pilot.press("u")
        await pilot.pause(0.2)
        assert not _shown(app, "#usage")
        assert app.query_one("#left").region.width == app.size.width  # the left column takes it all
    _run_app(tmp_path, monkeypatch, Settings(), steps)


def test_with_both_on_the_usage_graph_scrolls_and_the_project_graph_stays_in_view(tmp_path, monkeypatch):
    async def steps(app, pilot):
        await pilot.pause(0.5)
        app.render_usage()
        await pilot.pause(0.3)
        scroll, projects, usage_col = app.query_one("#usage-scroll"), app.query_one("#projects"), app.query_one("#usage")
        assert scroll.max_scroll_y > 0
        assert usage_col.region.y < projects.region.y < app.query_one("#farm").region.y
        scroll.scroll_end(animate=False)
        await pilot.pause(0.2)
        assert scroll.scroll_y > 0
        # tab still goes from the tree to the expiring pane, past the scrolling part
        await pilot.press("tab")
        await pilot.pause(0.1)
        assert app.focused and app.focused.id == "expiring"
    _run_app(tmp_path, monkeypatch, Settings(show_usage=True, show_projects=True), steps, size=(140, 36))


def test_plan_limits_are_fetched_for_the_project_graph_alone():
    app = main.AgentFarm(settings=Settings(show_limits=True, show_projects=True))
    calls = []
    app.fetch_limits = lambda: calls.append(1)
    app.usage_forced = False
    app._tick_limits(force=True)
    assert calls == [1]
    app.projects_forced = False
    app._tick_limits(force=True)
    assert calls == [1]                                               # nobody can see them


def test_the_project_graph_setting_round_trips(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_FARM_SETTINGS", str(tmp_path / "settings.json"))
    assert Settings.load().show_projects is False
    s = Settings.load()
    s.show_projects = True
    s.save()
    assert Settings.load().show_projects is True


class DayScanner(FakeScanner):
    """Fourteen days ending today, each with a weight, all of them since the week began."""

    def __init__(self, weights):
        super().__init__({"acme/a": Counter({"fable-5-1": float(sum(weights))})})
        today = datetime.fromtimestamp(NOW).date()
        self.days = [DayUsage(date=(today - timedelta(days=13 - i)).isoformat(), weight=w)
                     for i, w in enumerate(weights)]

    def report(self, n):
        return self.days


def _day_values(text):
    return [line.rsplit(None, 1)[-1] for line in text.plain.splitlines()
            if re.match(r"^(Mon|Tue|Wed|Thu|Fri|Sat|Sun) ", line)]


def test_limit_use_is_the_first_measure_and_the_days_add_up_to_claude_codes_part_of_the_week(monkeypatch):
    monkeypatch.setattr(main, "_model_slot", {})
    assert main.USAGE_METRICS[0][0] == "weight" and main.AgentFarm().usage_metric == 0
    weights = [0] * 10 + [10.0, 0, 30.0, 60.0]
    text = main.usage_text(DayScanner(weights), 0, 48, _report(), True, now=NOW)
    assert text.plain.splitlines()[0] == "Usage · last 14 days · est. % of weekly limit"
    values = _day_values(text)
    assert values[-4:] == ["3.3%", "0", "10.0%", "20.0%"]          # of 33.32: chats and cowork are the rest
    assert values[:10] == ["0"] * 10
    assert "$" not in "".join(text.plain.splitlines()[:-1])        # only the logged-cost line may say dollars
    assert all(len(line) <= 48 for line in text.plain.splitlines())


def test_without_a_current_week_limit_use_is_each_days_share_of_the_14_days(monkeypatch):
    monkeypatch.setattr(main, "_model_slot", {})
    text = main.usage_text(DayScanner([0] * 12 + [1.0, 3.0]), 0, 48, _report(), False, now=NOW)
    assert text.plain.splitlines()[0] == "Usage · last 14 days · est. share of use"
    assert _day_values(text)[-2:] == ["25.0%", "75.0%"]
    nothing = main.usage_text(DayScanner([0] * 14), 0, 48, _report(), False, now=NOW)
    assert _day_values(nothing) == ["0"] * 14


def test_m_still_reaches_the_counts(monkeypatch):
    monkeypatch.setattr(main, "_model_slot", {})
    scan = DayScanner([0] * 13 + [1.0])
    scan.days[-1].out_tokens = 1234
    assert main.usage_text(scan, 1, 48, _report(), True, now=NOW).plain.splitlines()[0].endswith("output tokens")
    assert _day_values(main.usage_text(scan, 1, 48, _report(), True, now=NOW))[-1] == "1k"


def test_each_day_keeps_what_its_messages_were_worth(tmp_path, monkeypatch):
    a = _repo(tmp_path / "a", "git@github.com:acme/a.git")
    now = time.time()
    u = _scanner(tmp_path, monkeypatch, {"one.jsonl": [_assistant("m1", str(a), now), _assistant("m1", str(a), now),
                                                       _assistant("m2", str(a), now, model="claude-opus-5-5", out=10)]})
    assert u.report(1)[-1].weight == 100 * 50 + 10 * 20               # once per message, at its model's weight


def _day_rows(text):
    return [line for line in text.plain.splitlines() if re.match(r"^(Mon|Tue|Wed|Thu|Fri|Sat|Sun) ", line)]


def test_each_limit_week_in_the_graph_has_its_own_fill_under_every_measure(monkeypatch):
    monkeypatch.setattr(main, "_model_slot", {})
    scan = DayScanner([float(i + 1) for i in range(14)])
    for d in scan.days:
        d.out_tokens = 1000
    week_start = RESET.timestamp() - 7 * 86400

    def back(d):
        start = datetime.strptime(d.date, "%Y-%m-%d")
        middle = (start.timestamp() + (start + timedelta(days=1)).timestamp()) / 2
        return 0 if middle >= week_start else 1 if middle >= week_start - 7 * 86400 else 2
    weeks = [back(d) for d in scan.days]
    assert set(weeks) == {0, 1, 2}
    last, earlier = main.HATCHES[1][0], main.HATCHES[2][0]
    assert (last, earlier) == ("▊", "╳")
    assert last not in "░▒▓"                # the app's terminal paints those as flat tints, with no gaps
    for measure in (0, 1):
        text = main.usage_text(scan, measure, 48, _report(), True, now=NOW)
        rows = _day_rows(text)
        for row, w in zip(rows, weeks):
            bar = row[11:]
            cells = set(bar.split()[0])         # a solid bar can end in the same eighth as last week's fill
            assert (cells == {last}, cells == {earlier}, "█" in cells or "▏" in cells) == (w == 1, w == 2, w == 0)
        lines = text.plain.splitlines()
        key = lines[lines.index(rows[-1]) + 1]
        assert key == f"{last * 2} last week · {earlier * 2} two weeks ago"
        assert all(len(line) <= 48 for line in lines)
    # only the weeks that are drawn are named
    only_last = DayScanner([0.0] * 3 + [1.0] * 11)
    lines = main.usage_text(only_last, 0, 48, _report(), True, now=NOW).plain.splitlines()
    assert f"{last * 2} last week" in lines and not any("two weeks ago" in line for line in lines)


def test_without_a_current_week_nothing_is_hatched(monkeypatch):
    monkeypatch.setattr(main, "_model_slot", {})
    scan = DayScanner([1.0] * 14)
    for limits_on, rep, now in ((False, _report(), NOW), (True, LimitsReport(), NOW),
                                (True, _report(), RESET.timestamp() + 60)):
        rows = _day_rows(main.usage_text(scan, 0, 48, rep, limits_on, now=now))
        assert len(rows) == 14
        assert not any(glyph in row[11:] for row in rows for glyph, _ in main.HATCHES.values())


def test_a_hatched_bar_keeps_its_length_in_whole_cells():
    assert main._hatched_bar(0.5, 10, "╳") == "╳" * 5 + " " * 5
    assert main._hatched_bar(0.001, 10, "╱") == "╱" + " " * 9
    assert main._hatched_bar(1.0, 4, "╳") == "╳" * 4
    assert main._hatched_bar(0, 4, "╳") == " " * 4


def test_a_reset_time_that_drifts_across_midnight_hatches_the_same_days(monkeypatch):
    """The server's reset time wobbles by fractions of a second (07:00:00.09, then 06:59:59.72).
    Where the week starts at local midnight, that must not flip the day before it."""
    monkeypatch.setattr(main, "_model_slot", {})
    local_midnight = datetime.fromtimestamp(NOW).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=3)
    hatched = []
    for wobble in (0.093696, -0.282968):
        reset = datetime.fromtimestamp(local_midnight.timestamp() + 7 * 86400 + wobble, timezone.utc)
        rep = LimitsReport((Window("weekly_all:", "week", 34.0, reset, 7 * 86400),), "", NOW,
                           "", (("claude code", 100.0),))
        rows = _day_rows(main.usage_text(DayScanner([1.0] * 14), 0, 48, rep, True, now=NOW))
        hatched.append([any(glyph in row[11:] for glyph, _ in main.HATCHES.values()) for row in rows])
    assert hatched[0] == hatched[1]
    assert hatched[0][-4:] == [False] * 4 and hatched[0][-5]          # the three days and today; the day before is not


def test_the_week_bar_is_split_by_model_like_the_output_bar(monkeypatch):
    monkeypatch.setattr(main, "_model_slot", {})
    models = [("fable-5-1", 90), ("opus-5-5", 8), ("fable-5", 5), ("haiku-5-5", 1)]
    split = {"acme/a": Counter({"fable-5-1": 6.0, "opus-5-5": 2.0}), "acme/b": Counter({"haiku-5-5": 2.0})}
    parts = main.week_parts(FakeScanner(split, models), _report(), True, now=NOW)
    blue, blue_glyph = main.MODEL_SLOTS[0]
    orange = main.MODEL_SLOTS[1][0]
    assert [p[0] for p in parts] == [blue, orange, main.MODEL_OTHER[0], main.SURFACE_BAR[0]]   # the bar's order, gray last
    assert abs(sum(p[2] for p in parts) - 1.0) < 1e-9
    assert abs(parts[0][2] - 0.98 * 0.6) < 1e-9 and abs(parts[-1][2] - 0.02) < 1e-9
    assert list(main._model_slot) == ["fable-5-1", "opus-5-5", "fable-5"]                     # nothing new handed out
    bar = main._limit_bar(40, 20, None, "normal", parts=parts)
    styles = [sp.style for sp in bar.spans]
    assert styles[:2] == [blue, orange] and bar.plain.startswith(blue_glyph * 4)
    assert len(bar.plain) == 20 and bar.plain.endswith("·")


def test_the_week_bar_keeps_its_pace_notch_and_still_turns_orange(monkeypatch):
    monkeypatch.setattr(main, "_model_slot", {})
    blue, orange = main.MODEL_SLOTS[0][0], main.MODEL_SLOTS[1][0]
    parts = [(blue, "█", 0.5), (orange, "▓", 0.5)]
    notched = main._limit_bar(80, 20, elapsed=0.6, severity="normal", parts=parts)
    notch = next(sp.style for sp in notched.spans if notched.plain[sp.start:sp.end] == main.PACE_TICK)
    assert notch.bgcolor == main.Style.parse(orange).color                     # cut into the cell it sits on
    warned = main._limit_bar(40, 20, severity="warning", parts=parts)
    assert {sp.style for sp in warned.spans} - {"dim"} == {main.LIMIT_WARN}
    assert {sp.style for sp in main._limit_bar(40, 20, parts=[]).spans} - {"dim"} == {main.BAR_HUE_TODAY}


def test_the_week_bar_is_green_without_a_current_week_or_use(monkeypatch):
    monkeypatch.setattr(main, "_model_slot", {})
    scan = FakeScanner({"acme/a": Counter({"fable-5-1": 1.0})})
    assert main.week_parts(scan, _report(), False, now=NOW) == []
    assert main.week_parts(scan, _report(), True, now=RESET.timestamp() + 60) == []
    assert main.week_parts(FakeScanner({}), _report(), True, now=NOW) == []


def test_the_panel_draws_the_week_bar_in_model_colors(monkeypatch):
    monkeypatch.setattr(main, "_model_slot", {})
    text = main.usage_text(DayScanner([1.0] * 14), 0, 48, _report(), True, now=NOW)
    plain = text.plain
    start = plain.index("\nweek ") + 1
    end = plain.index("\n", start)
    assert plain[start:end].endswith("34%   3d 0h")
    on_week = {sp.style for sp in text.spans if sp.start < end and sp.end > start}
    assert main.MODEL_SLOTS[0][0] in on_week and main.BAR_HUE_TODAY not in on_week
