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
    data = dict(REPLY, limits=[{"kind": "session", "percent": 3}, scoped, {"kind": "weekly_scoped"},
                               dict(scoped, scope={"model": {"display_name": "Opus"}})])
    data["seven_day_opus"] = {"utilization": 10, "resets_at": None}
    rep = parse_report(data, "max", now=1.0)
    assert [(w.label, int(w.used_pct)) for w in rep.windows] == [
        ("session", 42), ("week", 15), ("week · opus", 10), ("week · fable", 95)]   # no duplicate opus
    assert rep.windows[-1].resets_at == datetime(2026, 10, 17, 7, tzinfo=timezone.utc)
    assert main.limits_text(rep, True, 48).plain.splitlines()[-1].startswith("week · fable")


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
    assert main._limit_bar(80, 10).spans[0].style == main.STATUS_STYLE["waiting"]
    assert main._limit_bar(95, 10).spans[0].style == main.STATUS_STYLE["error"]
