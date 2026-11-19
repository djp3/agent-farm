"""The patterns that choose which sessions go to agent-grange: what a pattern matches, the
sample strings drawn from it, how the setting is stored, and the settings panel's list,
where each pattern has an on/off button and shows strings it matches."""
import asyncio
import json
import logging
import re
import signal
import threading
import time

import pytest

import grange
import main
from settings import Settings

PATTERNS = [r"proj-\d+-pr-\d+", r"^review", r"(?i)fix|rev(iew)?", r"[^a-z]x", r"(a|b)\1c",
            r"foo(?=bar)bar", r"foo(?!bar)", r"\w{2,}\s\S$", r"[éè]+", r"x?", r".*",
            r"[A-Z]{2}-\d{3,5}", r"(?P<n>ab)(?P=n)", r"\bpr\b", r"(?>ab)c*+", r"a.b+?c",
            r"[一-鿿]{2}", r"(x)?(?(1)y|z)", r"[^\W\d]+"]


# ---- the rules ----------------------------------------------------------

def test_check_names_what_is_wrong_with_a_pattern():
    assert grange.check(r"proj-\d+") is None
    assert "unterminated" in grange.check("(oops")
    assert grange.check("") == "empty"
    assert grange.check("   ") == "empty"
    assert "200" in grange.check("a" * 201)
    for huge in ("a{9999999999}", "a{1,9999999999}", "a{4294967296}"):    # OverflowError, not re.error
        assert "too large" in grange.check(huge)
        assert grange.samples(huge) == []
        assert not grange.reported("aaa", [{"pattern": huge, "enabled": True}])


def test_a_session_is_reported_when_a_pattern_that_is_on_matches_its_name():
    rules = [{"pattern": r"proj-\d+-pr-\d+", "enabled": True},
             {"pattern": r"^scratch", "enabled": False},
             {"pattern": "(broken", "enabled": True}]
    assert grange.reported("proj-86-pr-98 fix tests", rules)       # anywhere in the name
    assert grange.reported("tidy proj-3-pr-4", rules)
    assert not grange.reported("scratch pad", rules)                # that pattern is off
    assert not grange.reported("(broken", rules)                    # an invalid pattern never matches
    assert not grange.reported("anything", [])                      # no patterns: nothing reported
    assert not grange.reported("x review", [{"pattern": "^review", "enabled": True}])


def test_the_stored_list_keeps_good_entries_and_drops_the_rest():
    raw = [{"pattern": "a+", "enabled": False},
           {"pattern": "(kept", "enabled": True},           # invalid but kept, so a hand edit survives
           {"pattern": "b"},                                # no "enabled": on
           {"pattern": "c", "enabled": "yes"},              # not a bool: on
           {"pattern": ""}, {"pattern": 7}, "d", None, {"enabled": True}]
    assert grange.clean(raw) == [{"pattern": "a+", "enabled": False}, {"pattern": "(kept", "enabled": True},
                                 {"pattern": "b", "enabled": True}, {"pattern": "c", "enabled": True}]
    assert grange.clean("a+") == []


# ---- samples --------------------------------------------------------------

@pytest.mark.parametrize("pattern", PATTERNS)
def test_every_sample_matches_its_pattern(pattern):
    found = grange.samples(pattern)
    assert found, pattern
    assert len(found) == len(set(found)) <= grange.SAMPLE_COUNT
    for s in found:
        assert re.search(pattern, s), (pattern, s)
        assert len(s) <= grange.SAMPLE_MAX


def test_samples_are_the_same_every_time():
    assert grange.samples(r"proj-\d+-pr-\d+") == grange.samples(r"proj-\d+-pr-\d+")


def test_samples_read_like_names():
    assert grange.samples(r"proj-\d+-pr-\d+")[0].startswith("proj-")
    for s in grange.samples(r"a.b") + grange.samples(r"x\w\w"):
        assert s.isascii() and s.isalnum()                  # . and \w draw letters and digits
    assert all(s[0].isdigit() for s in grange.samples(r"\d+"))
    assert set(grange.samples(r"(?i)fix|rev(iew)?")) <= {"fix", "rev", "review"}


def test_no_samples_for_an_invalid_pattern_or_one_that_needs_a_long_string():
    assert grange.samples("(oops") == []
    assert grange.samples("a{500}") == []
    assert grange.samples(r"(?=x)y") == []                   # no draw satisfies the lookahead


SLOW = r"(?!(a+)+b)a{40}"           # backtracks catastrophically on every string it draws


def test_a_pattern_that_backtracks_badly_gets_a_note_instead_of_freezing_the_panel():
    began = time.monotonic()
    assert grange.samples(SLOW) is None
    assert time.monotonic() - began < 2                 # stopped by the time limit, once
    assert grange.samples(SLOW) is None                 # remembered: no second wait
    assert time.monotonic() - began < 2
    assert "took too long to try" in main.grange_samples_text(SLOW).plain


def test_a_pattern_that_asks_for_endless_drawing_stops_on_its_step_budget():
    nested = "(?:(?:(?:(?:(?:(?:^){40}){40}){40}){40}){40}){40}"     # 40**6 anchors, no characters
    began = time.monotonic()
    assert grange.samples(nested) == []                  # stopped by the budget, not the timer:
    assert time.monotonic() - began < grange.TIME_LIMIT_S / 2
    assert "no short example" in main.grange_samples_text(nested).plain    # not "backtracks badly"
    assert grange.samples(r"(?i)fix|rev(iew)?") == ["rev", "review", "fix"]   # busy normal ones unaffected


def test_a_name_too_slow_to_check_is_not_reported_and_the_pattern_is_logged_once(caplog, monkeypatch):
    monkeypatch.setattr(grange, "_slow_warned", set())
    rules = [{"pattern": r"(a+)+b", "enabled": True}]
    name = "a" * 40 + " zebra-42"
    began = time.monotonic()
    with caplog.at_level(logging.WARNING, logger="agent_farm.grange"):
        assert not grange.reported(name, rules)
        assert not grange.reported(name, rules)
    assert time.monotonic() - began < 2
    said = [r.getMessage() for r in caplog.records if "took over" in r.getMessage()]
    assert len(said) == 1 and "(a+)+b" in said[0] and "zebra" not in said[0]
    assert grange.reported("aab", rules)                # a name it is quick on still matches


def test_the_timer_is_put_away_after_each_use():
    rang = []
    mine = lambda signum, frame: rang.append(signum)    # noqa: E731
    before = signal.signal(signal.SIGALRM, mine)
    try:
        assert grange.reported("proj-1", [{"pattern": "proj", "enabled": True}])
        assert grange.samples(r"timer-\d{3}")
        assert signal.getsignal(signal.SIGALRM) is mine
        assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)
        time.sleep(grange.TIME_LIMIT_S + 0.1)
        assert rang == []
    finally:
        signal.signal(signal.SIGALRM, before)


def test_off_the_main_thread_nothing_runs_without_a_time_limit():
    out = {}

    def work():
        out["reported"] = grange.reported("proj-1", [{"pattern": "proj", "enabled": True}])
        out["samples"] = grange.samples(r"thread-\d")

    t = threading.Thread(target=work)
    t.start()
    t.join(5)
    assert out == {"reported": False, "samples": None}  # the safe answers
    assert grange.samples(r"thread-\d")                 # and that answer isn't kept for the panel


# ---- the setting ------------------------------------------------------------

def test_the_patterns_are_saved_and_read_back(tmp_path, monkeypatch):
    p = tmp_path / "settings.json"
    monkeypatch.setenv("AGENT_FARM_SETTINGS", str(p))
    assert Settings.load().grange_patterns == []
    s = Settings()
    s.grange_patterns = [{"pattern": r"proj-\d+", "enabled": True}, {"pattern": "^wip", "enabled": False}]
    s.save()
    assert Settings.load().grange_patterns == s.grange_patterns
    p.write_text(json.dumps({"grange_patterns": [{"pattern": "ok"}, 3, {"pattern": None}]}))
    assert Settings.load().grange_patterns == [{"pattern": "ok", "enabled": True}]
    p.write_text(json.dumps({"grange_patterns": "proj"}))
    assert Settings.load().grange_patterns == []


def test_the_sample_line_never_breaks_a_sample_across_lines():
    full = main.grange_samples_text(r"^review [a-z]+")
    assert full.plain.startswith("matches names containing")
    for pattern in (r"^review [a-z]+", "a{40}", "[一-鿿]{12}", r"proj-\d+-pr-\d+", r"x?"):
        for width in range(20, 121, 5):
            t = main.grange_samples_text(pattern, width)
            if "e.g." not in t.plain:
                assert t.plain == "its examples are too wide to show at this width"
                continue
            assert t.cell_len <= width, (pattern, width, t.plain)       # measured in cells
            shown = t.plain.split("e.g.  ", 1)[1].split("  ·  ")
            assert all(s in grange.samples(pattern) or s == "(nothing)" for s in shown)   # whole ones only
    assert main.grange_samples_text("a{40}", 50).plain == "its examples are too wide to show at this width"
    assert "a" * 40 in main.grange_samples_text("a{40}", 72).plain
    wide = main.grange_samples_text("[一-鿿]{12}", 75)                   # 32 + 24 cells fit; a second doesn't
    assert wide.cell_len <= 75 and wide.plain.count("  ·  ") == 0
    assert "not a valid pattern" in main.grange_samples_text("(oops").plain


# ---- the settings panel ------------------------------------------------------------

def _run(tmp_path, monkeypatch, steps, saved=None):
    """Open the settings panel on a fresh settings file (optionally holding `saved`) and
    run steps(app, pilot) there."""
    p = tmp_path / "settings.json"
    monkeypatch.setenv("AGENT_FARM_SETTINGS", str(p))
    if saved is not None:
        p.write_text(json.dumps({"grange_patterns": saved}))

    async def go():
        app = main.AgentFarm()
        app.collector.snapshot = lambda: []
        async with app.run_test(size=(110, 60)) as pilot:
            await pilot.pause(0.2)
            await pilot.press("s")
            await pilot.pause(0.2)
            assert isinstance(app.screen, main.SettingsScreen)
            await steps(app, pilot)
            await pilot.press("escape")
            await pilot.press("q")

    asyncio.run(go())
    return json.loads(p.read_text())["grange_patterns"] if p.exists() else None


def _texts(app, selector):
    return [str(w.render()) for w in app.screen.query(selector)]


def test_typing_a_pattern_shows_what_it_matches_and_enter_adds_it(tmp_path, monkeypatch):
    async def steps(app, pilot):
        assert _texts(app, "#grange-list SampleLine") == ["No patterns yet, so no session is reported."]
        box = app.screen.query_one("#grange-new")
        box.focus()
        await pilot.press(*"wip")                    # typed keys go to the field, not the panel's keys
        await pilot.pause(0.1)
        box.value = r"proj-\d+-pr-\d+"
        await pilot.pause(0.1)
        preview = str(app.screen.query_one("#grange-preview").render())
        assert "matches names containing" in preview and "proj-" in preview
        await pilot.press("enter")
        await pilot.pause(0.2)
        assert box.value == ""
        assert "as you type" in str(app.screen.query_one("#grange-preview").render())
        on = app.screen.query_one("#grange-on-0")
        assert str(on.label) == r"proj-\d+-pr-\d+" and on.value is True
        assert "proj-" in _texts(app, "#grange-list SampleLine")[0]
        box.value = r"[A-Z]{2}-\d+"                  # brackets show as typed, not as markup
        await pilot.click("#grange-add")
        await pilot.pause(0.2)
        assert str(app.screen.query_one("#grange-on-1").label) == r"[A-Z]{2}-\d+"

    assert _run(tmp_path, monkeypatch, steps) == [{"pattern": r"proj-\d+-pr-\d+", "enabled": True},
                                                 {"pattern": r"[A-Z]{2}-\d+", "enabled": True}]


def test_typing_s_or_q_in_the_field_does_not_leave_the_panel(tmp_path, monkeypatch):
    async def steps(app, pilot):
        app.screen.query_one("#grange-new").focus()
        await pilot.press("s", "q", "u", "p")
        await pilot.pause(0.1)
        assert isinstance(app.screen, main.SettingsScreen)
        assert app.screen.query_one("#grange-new").value == "squp"
        app.screen.query_one("#grange-new").value = ""

    _run(tmp_path, monkeypatch, steps)


def test_an_invalid_or_repeated_pattern_is_not_added(tmp_path, monkeypatch):
    async def steps(app, pilot):
        box = app.screen.query_one("#grange-new")
        box.focus()
        box.value = "(oops"
        await pilot.pause(0.1)
        assert "not a valid pattern" in str(app.screen.query_one("#grange-preview").render())
        await pilot.press("enter")
        await pilot.pause(0.1)
        assert "not added" in str(app.screen.query_one("#grange-preview").render())
        assert box.value == "(oops"                  # left to fix
        box.value = "a{9999999999}"                  # an overflow, not the usual pattern error
        await pilot.pause(0.1)
        assert "too large" in str(app.screen.query_one("#grange-preview").render())
        await pilot.press("enter")
        await pilot.pause(0.1)
        assert "not added" in str(app.screen.query_one("#grange-preview").render())
        assert app.is_running and isinstance(app.screen, main.SettingsScreen)
        box.value = "^wip"
        await pilot.press("enter")
        await pilot.pause(0.1)
        assert "already in the list" in str(app.screen.query_one("#grange-preview").render())
        box.value = ""

    assert _run(tmp_path, monkeypatch, steps, saved=[{"pattern": "^wip", "enabled": False}]) == \
        [{"pattern": "^wip", "enabled": False}]


def test_a_saved_pattern_that_backtracks_badly_does_not_freeze_the_panel(tmp_path, monkeypatch):
    async def steps(app, pilot):
        assert "took too long to try" in _texts(app, "#grange-list SampleLine")[0]

    began = time.monotonic()
    _run(tmp_path, monkeypatch, steps, saved=[{"pattern": r"(?!(a+)+b)a{39}", "enabled": False}])
    assert time.monotonic() - began < 10


def test_each_pattern_turns_on_and_off_and_can_be_removed(tmp_path, monkeypatch):
    saved = [{"pattern": "^wip", "enabled": True}, {"pattern": r"proj-\d+", "enabled": False},
             {"pattern": "(kept", "enabled": True}]

    async def steps(app, pilot):
        assert [b.value for b in app.screen.query("#grange-list RadioButton")] == [True, False, True]
        lines = _texts(app, "#grange-list SampleLine")
        assert "wip" in lines[0] and "proj-" in lines[1] and "not a valid pattern" in lines[2]
        await pilot.click("#grange-on-0")            # off
        await pilot.click("#grange-on-1")            # on
        await pilot.pause(0.1)
        assert [e["enabled"] for e in app.settings.grange_patterns] == [False, True, True]
        await pilot.click("#grange-rm-2")
        await pilot.pause(0.2)
        assert [str(b.label) for b in app.screen.query("#grange-list RadioButton")] == ["^wip", r"proj-\d+"]

    assert _run(tmp_path, monkeypatch, steps, saved=saved) == [{"pattern": "^wip", "enabled": False},
                                                              {"pattern": r"proj-\d+", "enabled": True}]
