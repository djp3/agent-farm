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
    rep = parse_report(REPLY, "max", now=time.time())
    lines = main.limits_text(rep, True, 48).plain.splitlines()
    assert lines[0].startswith("plan limits · max")
    assert "session" in lines[1] and " 42%" in lines[1] and "resets" in lines[1]
    assert "week" in lines[2] and " 15%" in lines[2]
    assert all(len(line) <= 48 for line in lines)
    failed = main.limits_text(LimitsReport(rep.windows, "max", rep.fetched_at, "network error"), True, 48).plain
    assert "unavailable: network error" in failed and "session" in failed
    paused = main.limits_text(rep, True, 48, polling=False).plain.splitlines()[0]
    assert paused.endswith("ago · paused") and len(paused) <= 48


def busy_farm(*statuses):
    from collector import Instance
    return [Instance(pid=i, session_id=f"s{i}", name=f"n{i}", alive=True, status=st)
            for i, st in enumerate(statuses, 1)]


def test_timer_polls_only_while_a_session_is_busy(monkeypatch):
    app = main.AgentFarm()                               # never run; the gate is plain logic
    calls = []
    monkeypatch.setattr(app, "fetch_limits", lambda: calls.append(1))
    app.settings.show_limits, app.usage_forced = True, True
    app.instances = busy_farm("idle", "waiting")
    app._tick_limits()
    assert calls == []                                   # everyone idle or waiting: no poll
    app._tick_limits(force=True)
    assert calls == [1]                                  # opening the panel still fetches once
    app.instances = busy_farm("idle", "busy")
    app._tick_limits()
    assert calls == [1, 1]
    app.usage_forced = False
    app._tick_limits(force=True)
    assert calls == [1, 1]                               # panel closed: nothing, not even forced
    app.usage_forced, app.settings.show_limits = True, False
    app._tick_limits()
    assert calls == [1, 1]                               # setting off: nothing
    assert main.LIMITS_POLL_S == 300.0


def test_reset_wording_today_vs_another_day():
    now = datetime(2026, 10, 13, 9, 0).astimezone()                  # naive = local time
    assert main.fmt_reset(datetime(2026, 10, 13, 15, 12).astimezone(), now) == "resets 3:12 PM"
    assert main.fmt_reset(datetime(2026, 10, 16, 10, 0).astimezone(), now) == "resets Fri 10 AM"
    assert main.fmt_reset(None, now) == ""


def test_bar_hue_follows_the_thresholds():
    assert main._limit_bar(42, 10).spans[0].style == main.BAR_HUE_TODAY
    assert main._limit_bar(80, 10).spans[0].style == main.STATUS_STYLE["waiting"]
    assert main._limit_bar(95, 10).spans[0].style == main.STATUS_STYLE["error"]
