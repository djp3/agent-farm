"""Plan limits (the /usage windows) come from Anthropic's OAuth usage endpoint using Claude
Code's own sign-in. The token must never reach the log or a process list, the reply must be
read tolerantly, and the panel must say why whenever the numbers are unavailable."""
import json
import logging
import subprocess
import time
from datetime import datetime, timezone

import limits
import main
from limits import LimitsReport, PlanLimits, fetch_usage, parse_report

REPLY = {
    "five_hour": {"utilization": 42.4, "resets_at": "2026-10-13T22:00:00Z"},
    "seven_day": {"utilization": 15.0, "resets_at": "2026-10-16T17:00:00+00:00"},
    "seven_day_opus": None,
    "seven_day_sonnet": {"utilization": None, "resets_at": None},
    "extra_usage": {"is_enabled": False, "utilization": None},
}
# The shape of a reply on a plan with no 5-hour window (seen 2026-11-03); the numbers are made up.
WEEK_END = "2026-11-07T07:00:00.093696+00:00"
ULTRA_REPLY = {
    "five_hour": None,
    "seven_day": {"utilization": 31.0, "resets_at": WEEK_END, "limit_dollars": None, "used_dollars": None,
                  "remaining_dollars": None, "locked_reason": None},
    "seven_day_opus": None,
    "seven_day_sonnet": None,
    "seven_day_breakdown": {"as_of": WEEK_END, "window_started_at": WEEK_END,
                            "rows": [{"key": "claude_code", "display_name": "Claude Code", "percent": 98},
                                     {"key": "chat", "display_name": "Chats", "percent": 1},
                                     {"key": "cowork", "display_name": "Cowork", "percent": 1},
                                     {"key": "other", "display_name": "Other", "percent": 0}]},
    "limits": [
        {"kind": "weekly_all", "group": "weekly", "percent": 31, "severity": "normal", "resets_at": WEEK_END,
         "scope": None, "is_active": False},
        {"kind": "weekly_scoped", "group": "weekly", "percent": 52, "severity": "normal", "resets_at": WEEK_END,
         "scope": {"model": {"id": None, "display_name": "Fable"}, "surface": None}, "is_active": True},
    ],
    "weekly_scoped_shares": [{"limit_index": 1, "used_percent_of_weekly": 26, "allowance_percent_of_weekly": 50}],
    "extra_usage": {"is_enabled": True, "monthly_limit": 10000, "used_credits": 0.0, "utilization": None,
                    "currency": "USD", "decimal_places": 2, "disabled_reason": None, "user_disabled": False,
                    "spend_limit_reached": False, "credits_ever_enabled": True, "daily": None, "weekly": None},
    "spend": {"used": {"amount_minor": 0, "currency": "USD", "exponent": 2},
              "limit": {"amount_minor": 10000, "currency": "USD", "exponent": 2},
              "percent": 0, "severity": "normal", "enabled": True, "disabled_reason": None},
}
TOKEN = "sk-ant-oat01-SECRET-TOKEN-VALUE"
CREDS = json.dumps({"claudeAiOauth": {"accessToken": TOKEN, "expiresAt": 4102444800000,
                                      "subscriptionType": "max"}})


def test_reply_parses_into_ordered_windows_and_skips_empty_ones():
    rep = parse_report(REPLY, "max", now=1000.0)
    assert [(w.label, int(w.used_pct)) for w in rep.windows] == [("session", 42), ("week", 15)]
    assert rep.windows[0].resets_at == datetime(2026, 10, 13, 22, tzinfo=timezone.utc)
    assert (rep.plan, rep.fetched_at, rep.error) == ("max", 1000.0, "")


def test_model_scoped_week_comes_from_the_limits_list():
    scoped = {"kind": "weekly_scoped", "group": "weekly", "percent": 95, "severity": "critical",
              "resets_at": "2026-10-17T07:00:00Z",
              "scope": {"model": {"id": None, "display_name": "Fable"}, "surface": None}}
    rep = parse_report(dict(REPLY, limits=[scoped, {"kind": "weekly_scoped"}]), "max", now=1.0)
    assert [(w.label, int(w.used_pct)) for w in rep.windows] == [("session", 42), ("week", 15), ("week · fable", 95)]
    assert rep.windows[-1].resets_at == datetime(2026, 10, 17, 7, tzinfo=timezone.utc)
    assert main.limits_text(rep, True, 48).plain.splitlines()[-1].startswith("week · fable")


def test_a_window_in_both_places_shows_once_from_the_limits_rows():
    opus = {"kind": "weekly_scoped", "percent": 95, "scope": {"model": {"display_name": "Opus"}}}
    data = dict(REPLY, seven_day_opus={"utilization": 10, "resets_at": None},
                limits=[opus, {"kind": "session", "percent": 3}])
    rep = parse_report(data, "max", now=1.0)
    assert [(w.label, int(w.used_pct)) for w in rep.windows] == [("session", 3), ("week", 15), ("week · opus", 95)]


def test_ultra_reply_has_no_session_and_keeps_the_extra_bar():
    now = datetime(2026, 11, 3, 7, 0, tzinfo=timezone.utc).timestamp()
    rep = parse_report(ULTRA_REPLY, "ultra", now=now)
    assert [(w.label, int(w.used_pct)) for w in rep.windows] == [("week", 31), ("week · fable", 52), ("extra", 0)]
    assert not rep.error
    assert rep.windows[0].elapsed(now) is not None and rep.windows[-1].elapsed(now) is None
    lines = main.limits_text(rep, True, 48, now=now).plain.splitlines()
    assert lines[0].startswith("plan limits · ultra") and len(lines) == 5
    assert not any("session" in line for line in lines)
    assert lines[3].startswith("extra") and lines[3].endswith("  0%")       # no clock, so no time left
    assert all(len(line) <= 48 for line in lines)


def test_a_models_week_wears_that_models_hue_from_the_output_bar(monkeypatch):
    monkeypatch.setattr(main, "_model_slot", {})
    split = [("opus-5-5", 90), ("fable-5-1", 80), ("fable-5", 70), ("haiku-5-5", 10)]
    hues = main.model_hues(split)
    assert list(hues) == ["opus-5-5", "fable-5-1", "fable-5"]          # what the bar names
    opus, fable = main.MODEL_SLOTS[0][0], main.MODEL_SLOTS[1][0]
    drawn = {s.style for s in main.limits_text(parse_report(ULTRA_REPLY, "", 1.0), True, 48, now=1.0, hues=hues).spans}
    assert fable in drawn and main.BAR_HUE_TODAY in drawn              # the biggest Fable version; the week stays green
    assert opus not in drawn
    assert main._calm_hue("week · opus", hues) == opus
    # drawing the bar afterwards hands out the same slots, in the same order
    before = dict(main._model_slot)
    assert fable in {s.style for s in main.model_split_text(split, 48).spans}
    assert main._model_slot == before
    # the whole week, spend, a surface and a model folded into Other have no model hue
    for label in ("week", "extra", "week · claude design", "week · haiku", "week · fa"):
        assert main._calm_hue(label, hues) == main.BAR_HUE_TODAY
    assert main._calm_hue("week · fable", {}) == main.BAR_HUE_TODAY


def test_a_models_week_still_turns_orange_and_red_when_it_needs_attention():
    blue = main.MODEL_SLOTS[0][0]
    assert main._limit_bar(40, 10, calm=blue).spans[0].style == blue
    assert main._limit_bar(40, 10, severity="normal", calm=blue).spans[0].style == blue
    assert main._limit_bar(40, 10, severity="warning", calm=blue).spans[0].style == main.LIMIT_WARN
    assert main._limit_bar(80, 10, calm=blue).spans[0].style == main.LIMIT_WARN
    assert main._limit_bar(95, 10, calm=blue).spans[0].style == main.LIMIT_OVER
    assert main._limit_bar(95, 10, severity="normal", calm=blue).spans[0].style == blue


def test_the_weeks_use_by_surface_follows_the_bars():
    rep = parse_report(ULTRA_REPLY, "ultra", now=1.0)
    assert rep.surfaces == (("claude code", 98), ("chats", 1), ("cowork", 1))      # 0% is left out
    lines = main.limits_text(rep, True, 48, now=1.0).plain.splitlines()
    assert lines[-1] == "used in claude code 98% · chats 1% · cowork 1%"
    # a reply without the breakdown, or with nothing used yet, has no such line
    assert parse_report(REPLY, "", 1.0).surfaces == ()
    assert not any("used in" in line for line in main.limits_text(parse_report(REPLY, "", 1.0), True, 48).plain.splitlines())
    for odd in (None, "odd", {}, {"rows": "odd"}, {"rows": ["odd", {"percent": 5}, {"display_name": "x", "percent": 0},
                                                            {"display_name": 3, "percent": 9}]}):
        assert parse_report(dict(REPLY, seven_day_breakdown=odd), "", 1.0).surfaces == ()


def test_many_surfaces_wrap_between_names_and_stay_in_the_panel():
    many = (("claude code", 60), ("chats", 20), ("cowork", 10), ("other", 5), ("a surface name here", 5))
    lines = main._surface_lines(many, 30)
    assert lines == ["used in claude code 60%", "        chats 20% · cowork 10%", "        other 5%",
                     "        a surface name here 5%"]
    assert all(len(line) <= 30 for line in lines)


def test_extra_share_comes_from_whichever_shape_the_reply_has():
    def extra(**data):
        rep = parse_report(dict({"seven_day": {"utilization": 1}}, **data), "", 1.0)
        return next((w.used_pct for w in rep.windows if w.label == "extra"), None)
    assert extra(extra_usage={"is_enabled": True, "utilization": 7.5}) == 7.5
    assert extra(extra_usage={"is_enabled": True, "utilization": None}, spend={"percent": 12}) == 12
    assert extra(extra_usage={"is_enabled": True, "used_credits": 2500.0, "monthly_limit": 10000}) == 25
    assert extra(spend={"enabled": True, "percent": 9}) == 9
    assert extra(extra_usage={"is_enabled": False, "utilization": 7.5}, spend={"enabled": True, "percent": 9}) is None
    assert extra(spend={"enabled": False, "percent": 9}) is None
    # on but with no limit to be a share of: Claude Code says "Unlimited"
    assert extra(extra_usage={"is_enabled": True, "used_credits": 2500.0, "monthly_limit": None}) is None
    assert extra(extra_usage={"is_enabled": True, "used_credits": 2500.0, "monthly_limit": 0}) is None
    assert extra(extra_usage="odd", spend=["odd"]) is None


def test_a_meter_this_version_has_not_heard_of_still_gets_a_bar():
    now = datetime(2026, 11, 3, tzinfo=timezone.utc).timestamp()
    rows = [{"kind": "monthly_all", "group": "monthly", "percent": 5, "resets_at": "2026-12-01T00:00:00Z"},
            {"kind": "monthly_scoped", "group": "monthly", "percent": 6, "severity": 3,
             "scope": {"surface": {"display_name": "Workbenches\x07Extended"}}},
            {"percent": 7}, "odd", {"kind": "daily_all", "percent": True}]
    rep = parse_report({"limits": rows}, "", now)
    assert [w.label for w in rep.windows] == ["monthly", "monthly · workbenche"]
    assert rep.windows[0].elapsed(now) is None and rep.windows[1].severity == ""     # no length, so no pace tick
    lines = main.limits_text(rep, True, 48, now=now).plain.splitlines()
    assert lines[1].endswith("28d 0h") and all(len(line) <= 48 for line in lines)


def test_the_server_reading_colors_a_window_when_it_gives_one():
    assert main._limit_bar(10, 10, severity="critical").spans[0].style == main.LIMIT_OVER
    assert main._limit_bar(10, 10, severity="warning").spans[0].style == main.LIMIT_WARN
    assert main._limit_bar(95, 10, severity="normal").spans[0].style == main.BAR_HUE_TODAY
    assert main._limit_bar(95, 10, severity="new-word").spans[0].style == main.LIMIT_OVER


def test_odd_replies_degrade_without_crashing():
    assert parse_report({"foo": 1}, "", 1.0).error
    assert parse_report({"five_hour": {"utilization": 250}}, "", 1.0).windows[0].used_pct == 100.0
    extra = dict(REPLY, extra_usage={"is_enabled": True, "utilization": 7.5})
    assert parse_report(extra, "", 1.0).windows[-1].label == "extra"


def test_credentials_parse_milliseconds_and_reject_other_shapes():
    tok = limits._parse_credentials(CREDS)
    assert tok.value == TOKEN and tok.plan == "max" and 4102444700 < tok.expires_at < 4102444900
    assert limits._parse_credentials("{not json") is None
    assert limits._parse_credentials('{"claudeAiOauth": {"accessToken": 5}}') is None


def test_plan_name_comes_from_the_profile_and_only_that_is_read(tmp_path, monkeypatch, caplog):
    creds, config = tmp_path / ".credentials.json", tmp_path / ".claude.json"
    creds.write_text(CREDS)
    monkeypatch.setattr(limits, "CREDENTIALS_FILE", creds)
    monkeypatch.setattr(limits, "GLOBAL_CONFIG", config)
    assert limits.load_token()[0].plan == "max"                        # no file: the sign-in's type
    config.write_text(json.dumps({"oauthAccount": {"planDisplayName": "Ultra", "emailAddress": "me@example.com"}}))
    with caplog.at_level(logging.DEBUG, logger="agent_farm.limits"):
        tok, why = limits.load_token()
    assert (tok.plan, tok.value, why) == ("ultra", TOKEN, "")
    assert "me@example.com" not in caplog.text and "Ultra" not in caplog.text
    for odd in ("{not json", "[]", '{"oauthAccount": null}', '{"oauthAccount": {"planDisplayName": 7}}',
                '{"oauthAccount": {"planDisplayName": ""}}'):
        config.write_text(odd)
        assert limits.load_token()[0].plan == "max"


def test_token_goes_to_curl_on_stdin_never_in_argv(monkeypatch):
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"], seen["input"] = cmd, kw.get("input", "")
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(REPLY) + "\n200", stderr="")
    monkeypatch.setattr(limits.subprocess, "run", fake_run)
    assert fetch_usage(TOKEN) == (REPLY, "")
    assert seen["cmd"][0] == limits.CURL and seen["cmd"][-1] == limits.USAGE_URL
    assert all(TOKEN not in a for a in seen["cmd"])
    assert f"Bearer {TOKEN}" in seen["input"] and limits.OAUTH_BETA in seen["input"]


def test_http_401_means_the_sign_in_expired(monkeypatch):
    monkeypatch.setattr(limits.subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(
        cmd, 0, stdout='{"error":"x"}\n401', stderr=""))
    assert fetch_usage(TOKEN) == (None, limits.EXPIRED)


def test_credentials_file_is_preferred_over_the_keychain(tmp_path, monkeypatch):
    f = tmp_path / ".credentials.json"
    f.write_text(CREDS)
    monkeypatch.setattr(limits, "CREDENTIALS_FILE", f)
    monkeypatch.setattr(limits, "GLOBAL_CONFIG", tmp_path / "absent.json")

    def no_keychain(*a, **k):
        raise AssertionError("security must not run")
    monkeypatch.setattr(limits.subprocess, "run", no_keychain)
    tok, why = limits.load_token()
    assert tok.value == TOKEN and why == ""


def test_refresh_keeps_last_windows_and_reports_the_reason(monkeypatch, caplog):
    pl = PlanLimits()
    monkeypatch.setattr(limits, "load_token", lambda: (limits._Token(TOKEN, 0.0, "max"), ""))
    replies = [(REPLY, ""), (None, "network error")]
    monkeypatch.setattr(limits, "fetch_usage", lambda token: replies.pop(0))
    with caplog.at_level(logging.DEBUG, logger="agent_farm.limits"):
        assert pl.refresh() is True
        assert pl.refresh() is False                   # too soon after a good fetch
        assert pl.refresh(force=True) is True          # the failure is recorded...
    rep = pl.report()
    assert [w.label for w in rep.windows] == ["session", "week"]    # ...but the bars stay
    assert rep.error == "network error"
    messages = [r.getMessage() for r in caplog.records]
    assert messages and all(TOKEN not in m and "42.4" not in m for m in messages)


def test_expired_token_is_reread_not_sent(monkeypatch):
    pl = PlanLimits()
    reads = []

    def expired_token():
        reads.append(1)
        return limits._Token(TOKEN, time.time() - 5, "max"), ""

    def no_request(token):
        raise AssertionError("no request with an expired token")
    monkeypatch.setattr(limits, "load_token", expired_token)
    monkeypatch.setattr(limits, "fetch_usage", no_request)
    pl.refresh()
    assert pl.report().error == limits.EXPIRED and reads == [1]


def test_panel_block_states():
    off = main.limits_text(None, False, 48).plain
    assert "off" in off and "Settings" in off
    assert "fetching" in main.limits_text(LimitsReport(), True, 48).plain
    now = datetime(2026, 10, 13, 19, 0, tzinfo=timezone.utc).timestamp()   # 3 h before the session resets
    rep = parse_report(REPLY, "max", now=now)
    lines = main.limits_text(rep, True, 48, now=now).plain.splitlines()
    assert lines[0].startswith("plan limits · max")
    assert "session" in lines[1] and " 42%" in lines[1] and lines[1].endswith(" 3h00m")
    assert "week" in lines[2] and " 15%" in lines[2] and lines[2].endswith(" 2d22h")
    assert all(main.PACE_TICK in line for line in lines[1:])
    assert all(len(line) <= 48 for line in lines)
    failed = main.limits_text(LimitsReport(rep.windows, "max", rep.fetched_at, "network error"), True, 48).plain
    assert "unavailable: network error" in failed and "session" in failed
    paused = main.limits_text(rep, True, 48, polling=False).plain.splitlines()[0]
    assert paused.endswith("ago · paused") and len(paused) <= 48


def farm(*statuses, agent=None):
    """Live sessions with the given statuses; agent=(index, status) adds a subagent to one."""
    from collector import Agent, Instance
    out = [Instance(pid=i, session_id=f"s{i}", name=f"n{i}", alive=True, status=st)
           for i, st in enumerate(statuses, 1)]
    if agent:
        idx, st = agent
        out[idx].agents = [Agent(agent_id="a1", name="helper", status=st)]
    return out


def test_working_means_busy_sessions_or_running_subagents():
    assert not main.fleet_working(farm("idle", "waiting"))
    assert main.fleet_working(farm("idle", "busy"))
    assert main.fleet_working(farm("idle", agent=(0, "running")))      # lead idle, subagent working
    assert main.fleet_working(farm("waiting", agent=(0, "starting")))
    assert not main.fleet_working(farm("idle", agent=(0, "dormant")))
    dead = farm("busy")
    dead[0].alive = False
    assert not main.fleet_working(dead)


class Clock:
    def __init__(self):
        self.t = 10_000.0

    def __call__(self):
        return self.t


def gated_app(monkeypatch):
    app = main.AgentFarm()                                # never run; the gate is plain logic
    calls = []
    monkeypatch.setattr(app, "fetch_limits", lambda: calls.append(clock.t))
    clock = Clock()
    monkeypatch.setattr(main, "_monotonic", clock)
    app.settings.show_limits, app.usage_forced = True, True
    return app, calls, clock


def test_no_fetch_while_everyone_is_idle_or_waiting(monkeypatch):
    app, calls, clock = gated_app(monkeypatch)
    for _ in range(20):
        app._limits_on_snapshot(farm("idle", "waiting"))
        clock.t += 60
    assert calls == []
    assert not app._limits_polling()                      # the header says "paused"


def test_first_fetch_comes_as_soon_as_work_resumes(monkeypatch):
    app, calls, clock = gated_app(monkeypatch)
    app._tick_limits(force=True)                          # the panel opened: one fetch
    clock.t += 3000                                       # a long idle stretch
    app._limits_on_snapshot(farm("idle"))
    assert len(calls) == 1
    app._limits_on_snapshot(farm("busy"))                 # work resumes: fetch now, not at a tick
    assert calls == [calls[0], clock.t]


def test_at_most_one_fetch_per_interval_while_working(monkeypatch):
    app, calls, clock = gated_app(monkeypatch)
    for _ in range(600):                                  # ten minutes of one-second snapshots
        app._limits_on_snapshot(farm("busy"))
        clock.t += 1
    assert len(calls) == 2
    assert calls[1] - calls[0] == main.LIMITS_POLL_S


def test_a_short_burst_between_checks_still_counts(monkeypatch):
    app, calls, clock = gated_app(monkeypatch)
    app._limits_on_snapshot(farm("busy"))                 # fetch 1
    clock.t += 30
    app._limits_on_snapshot(farm("busy"))                 # work after the fetch, too soon to fetch again
    clock.t += 30
    for _ in range(10):                                   # idle from here on
        app._limits_on_snapshot(farm("idle"))
        clock.t += 30
    assert len(calls) == 2                                # the burst was remembered and fetched once
    assert calls[1] - calls[0] >= main.LIMITS_POLL_S
    clock.t += 3600
    app._limits_on_snapshot(farm("idle"))
    assert len(calls) == 2                                # nothing new happened: no more fetches


def test_paused_marker_and_age_redraw_without_other_activity(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(main, "_monotonic", clock)
    monkeypatch.setattr(main.AgentFarm, "fetch_limits", lambda self: None)
    draws = []

    async def go():
        app = main.AgentFarm()
        app.collector.snapshot = lambda: []
        app.settings.show_limits = True
        async with app.run_test(size=(140, 44)) as pilot:
            await pilot.pause(0.2)
            app.usage_forced = True
            real = app.render_usage
            monkeypatch.setattr(app, "render_usage", lambda: (draws.append(clock.t), real()))
            app._limits_on_snapshot(farm("busy"))          # first look: draws
            n = len(draws)
            app._limits_on_snapshot(farm("busy"))          # nothing changed: no draw
            assert len(draws) == n
            clock.t += 1
            app._limits_on_snapshot(farm("idle"))          # work since the fetch: still not paused
            assert len(draws) == n
            app._tick_limits(force=True)                   # a fetch clears the latch...
            app._limits_on_snapshot(farm("idle"))          # ...so "paused" appears at once
            assert len(draws) == n + 1
            clock.t += main.LIMITS_REDRAW_S
            app._limits_on_snapshot(farm("idle"))          # the age is redrawn once a minute
            assert len(draws) == n + 2
            await pilot.press("q")

    import asyncio
    asyncio.run(go())


def test_hidden_panel_or_setting_off_never_fetches(monkeypatch):
    app, calls, clock = gated_app(monkeypatch)
    app.usage_forced = False
    app._limits_on_snapshot(farm("busy"))
    app._tick_limits(force=True)
    app.usage_forced, app.settings.show_limits = True, False
    app._limits_on_snapshot(farm("busy"))
    assert calls == []
    app.settings.show_limits = True                       # back on: the remembered work fetches
    app._limits_on_snapshot(farm("idle"))
    assert len(calls) == 1


def test_countdown_to_the_reset():
    at = datetime(2026, 10, 16, 10, 0, tzinfo=timezone.utc)
    left = lambda s: main.fmt_countdown(at, at.timestamp() - s)
    assert left(4 * 86400 + 5 * 3600 + 59) == "4d 5h"
    assert left(2 * 86400 + 19 * 3600) == "2d19h"
    assert left(13 * 3600 + 5 * 60) == "13h05m"
    assert left(2 * 3600 + 54 * 60 + 30) == "2h54m"
    assert left(38 * 60) == "38m"
    assert left(30) == "<1m"
    assert left(0) == left(-600) == "reset"          # the reading predates the reset
    assert main.fmt_countdown(None, 0) == ""


def test_elapsed_share_of_each_window():
    rep = parse_report(REPLY, "", now=1.0)
    session, week = rep.windows
    reset = session.resets_at.timestamp()
    assert session.elapsed(reset - 3 * 3600) == 0.4                   # 2 h into a 5-hour session
    assert week.elapsed(week.resets_at.timestamp() - 7 * 86400 / 4) == 0.75
    assert session.elapsed(reset - 9 * 3600) == 0.0                   # clock skew: clamp, no crash
    assert session.elapsed(reset) is None and session.elapsed(reset + 60) is None   # has reset since
    extra = parse_report(dict(REPLY, extra_usage={"is_enabled": True, "utilization": 7}), "", 1.0)
    assert extra.windows[-1].elapsed(reset - 60) is None             # extra spend has no clock
    scoped = {"kind": "weekly_scoped", "percent": 5, "resets_at": "2026-10-17T07:00:00Z",
              "scope": {"model": {"display_name": "Fable"}}}
    fable = parse_report(dict(REPLY, limits=[scoped]), "", 1.0).windows[-1]
    assert fable.span_s == limits.WEEK_S


def test_pace_tick_sits_where_an_even_pace_would_be():
    def cells(bar):
        return [(bar.plain[i], [sp.style for sp in bar.spans if sp.start <= i < sp.end]) for i in range(len(bar))]
    under = main._limit_bar(30, 20, elapsed=0.6)                       # behind the clock
    assert under.plain.index(main.PACE_TICK) == 12 and len(under) == 20
    assert cells(under)[12][1] == [main.PACE_TICK_ON_TRACK]           # bright, on the dotted track
    ahead = main._limit_bar(80, 20, elapsed=0.25)                      # ahead of the clock
    assert ahead.plain.index(main.PACE_TICK) == 5
    notch = cells(ahead)[5][1][0]
    assert notch.bgcolor.name == "#ff9500" and notch.color.name == main.PACE_TICK_NOTCH   # cut into the fill
    assert main._limit_bar(10, 20, elapsed=1.0).plain[-1] == main.PACE_TICK   # last moment stays in the bar
    assert main.PACE_TICK not in main._limit_bar(10, 20).plain


def test_bar_hue_follows_the_thresholds():
    assert main._limit_bar(42, 10).spans[0].style == main.BAR_HUE_TODAY
    assert main._limit_bar(80, 10).spans[0].style == main.LIMIT_WARN
    assert main._limit_bar(95, 10).spans[0].style == main.LIMIT_OVER
