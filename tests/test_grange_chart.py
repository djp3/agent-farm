"""The reports-to-agent-grange band under agents at work: the history it draws from (spans of
time with the grange's answer, a day of it kept beside the settings), the marks, and the app
showing it only when something was reported in the last day."""
import asyncio
import json
import logging
import time
from datetime import datetime

import pytest

import grange
import main
import usage
from collector import Instance
from expiring import ExpiryScanner
from settings import Settings

H = grange.HEARTBEAT_S
T0 = datetime(2026, 11, 20, 9, 0).timestamp()


# ---- the history --------------------------------------------------------------------

def test_pushes_with_one_answer_make_one_span_and_a_new_answer_starts_the_next():
    h = grange.History(None)
    for k in range(10):                                  # a heartbeat apart, give or take a tick
        h.answered(T0 + k * (H + 1), "accepted")
    assert h.spans == [[T0, T0 + 9 * (H + 1) + H, "accepted"]]
    t = T0 + 10 * (H + 1)
    assert h.answered(t, "out of date")
    assert h.spans == [[T0, t, "accepted"], [t, t + H, "out of date"]]
    assert not h.answered(t + H, "out of date")          # growing: nothing new to draw


def test_a_gap_longer_than_two_heartbeats_starts_a_new_span():
    h = grange.History(None)
    h.answered(T0, "accepted")
    h.answered(T0 + 2 * H + 5, "accepted")               # the monitor was closed, or the Mac slept
    assert h.spans == [[T0, T0 + H, "accepted"], [T0 + 2 * H + 5, T0 + 3 * H + 5, "accepted"]]


def test_turning_reporting_off_ends_the_span_at_once():
    h = grange.History(None)
    h.answered(T0, "accepted")
    assert h.stopped(T0 + 5)
    assert h.spans == [[T0, T0 + 5, "accepted"]]
    assert not h.stopped(T0 + 6)                          # nothing running: nothing to end


@pytest.mark.parametrize("kind", ["accepted", "refused"])
def test_reporting_turned_back_on_soon_after_starts_a_new_span(kind):
    """Off at 5 s and on again at 10 s: the five seconds between were not reporting, whether
    the next answer is the same as the last or not."""
    h = grange.History(None)
    h.answered(T0, "accepted")
    h.stopped(T0 + 5)
    assert h.answered(T0 + 10, kind)
    assert h.spans == [[T0, T0 + 5, "accepted"], [T0 + 10, T0 + 10 + H, kind]]
    assert not h.closed


def test_a_span_that_ran_out_a_moment_before_reporting_was_turned_off_is_closed_too():
    """A heartbeat can come a second late, and the span runs out in between."""
    h = grange.History(None)
    h.answered(T0, "accepted")
    assert h.stopped(T0 + H + 0.5) and h.spans == [[T0, T0 + H, "accepted"]]
    h.answered(T0 + H + 3, "accepted")
    assert len(h.spans) == 2


def test_turned_off_is_remembered_across_a_restart_and_a_restart_alone_is_no_gap(tmp_path):
    p = tmp_path / "grange-history.json"
    now = time.time()
    h = grange.History(p)
    h.answered(now - 60, "accepted")
    h.stopped(now - 55)
    assert json.loads(p.read_text())["closed"] is True
    again = grange.History(p)                            # the monitor restarted, reporting turned on
    again.answered(now - 50, "accepted")
    assert len(again.spans) == 2
    again.flush()
    after = grange.History(p)                            # restarted while reporting: one span carries on
    after.answered(now - 50 + H + 5, "accepted")
    assert len(after.spans) == 2 and after.spans[-1][1] == now - 50 + 2 * H + 5


def test_a_day_is_kept_in_a_file_with_only_times_and_answers(tmp_path):
    p = tmp_path / "grange-history.json"
    now = time.time()
    h = grange.History(p)
    h.answered(now - 30 * 3600, "accepted")              # older than the day: dropped
    h.answered(now - 3600, "accepted")
    h.answered(now - 3600 + H, "refused")
    data = json.loads(p.read_text())
    assert sorted(data) == ["closed", "spans"] and data["closed"] is False
    assert all(len(sp) == 3 and isinstance(sp[0], int) and isinstance(sp[1], int) for sp in data["spans"])
    assert [sp[2] for sp in data["spans"]] == ["accepted", "refused"]
    assert [sp[2] for sp in grange.History(p).spans] == ["accepted", "refused"]


def test_a_growing_span_is_saved_at_most_once_a_minute_and_the_rest_on_quit(tmp_path):
    p = tmp_path / "grange-history.json"
    now = time.time()
    h = grange.History(p)
    h.answered(now, "accepted")                          # a new span: saved at once
    first = json.loads(p.read_text())["spans"]
    h.answered(now + H, "accepted")                      # it grows: held back
    assert json.loads(p.read_text())["spans"] == first
    h.flush()
    assert json.loads(p.read_text())["spans"] == [[round(now), round(now + 2 * H), "accepted"]]


def test_a_damaged_file_starts_a_new_history_and_bad_entries_are_dropped(tmp_path, caplog):
    p = tmp_path / "grange-history.json"
    p.write_text("{not json")
    with caplog.at_level(logging.WARNING, logger="agent_farm.grange"):
        assert grange.History(p).spans == []
    now = float(round(time.time()))
    good = [now - 60, now - 30, "accepted"]
    p.write_text(json.dumps({"spans": [good, [now, now - 5, "accepted"], [now - 9, now], ["a", "b", "accepted"],
                                       [now - 60, now - 30, "gossip"], [True, now, "accepted"], "x"]}))
    assert grange.History(p).spans == [good]


# ---- the marks -----------------------------------------------------------------------

def test_an_hour_is_marked_by_how_long_reporting_was_on_and_the_answer_that_held_longest():
    starts = [T0 + 3600 * k for k in range(4)]
    now = starts[3] + 1800                                # half way through the last hour
    spans = [[starts[0], starts[1], "accepted"],                                  # all of hour 0
             [starts[1], starts[1] + 1200, "accepted"],                           # hour 1: 20 min taken,
             [starts[1] + 1200, starts[1] + 3000, "out of date"],                 # then 30 out of date
             [starts[3], starts[3] + 900, "refused"], [starts[3] + 900, now + H, "accepted"]]
    hours = main.grange_hours(spans, starts, now)
    assert hours[0] == (1.0, main.GREEN)
    assert hours[1] == (pytest.approx(50 / 60), main.YELLOW)
    assert hours[2] == (0.0, "")
    assert hours[3] == (1.0, main.RED)                    # all of the half hour so far; a tie goes to the worse


def test_each_mark_sits_under_its_hour_s_stalk_with_now_beside_them():
    bins = [(T0 - 3600 * (23 - k), v) for k, v in enumerate([0.0] * 20 + [1.0, 2.0, 3.0, 2.5])]
    starts = [a for a, _ in bins]
    now = starts[-1] + 1800
    spans = [[starts[20], starts[22], "accepted"], [starts[22] + 600, now + H, "not running"]]
    hours = main.grange_hours(spans, starts, now)
    chart = main.activity_text(bins, 47, now=now).plain.splitlines()
    _, _, gut = main._activity_scale([v for _, v in bins])
    band = main.grange_text(hours, starts, gut, ("3 sessions", ""), 47).plain.splitlines()
    assert band[0] == "reports to agent-grange · last 24 hours"
    bottom = chart[main.ACTIVITY_ROWS]                    # the stalks' bottom row
    row = band[1]
    for k in range(20, 24):
        assert bottom[gut + 1 + k] != " " and row[gut + 1 + k] in "●◐"
    assert row[gut + 1:gut + 1 + 24] == "·" * 20 + "●●◐●"
    assert row.endswith("now  3 sessions")
    assert band[2:4] == chart[1 + main.ACTIVITY_ROWS:3 + main.ACTIVITY_ROWS]   # the same axis and hours
    narrow = main.grange_text(hours, starts, gut, ("3 sessions", ""), 30).plain.splitlines()[1]
    assert narrow == row[:gut + 1 + 24]                   # no room: the figure goes, the marks stay


# ---- the app -------------------------------------------------------------------------

PROJ = [{"pattern": "^proj", "enabled": True}]


def _run(tmp_path, monkeypatch, steps, settings=None, spans=None, sessions=()):
    """The app with no transcripts to scan, and the history file under the test's settings."""
    monkeypatch.setattr(usage, "PROJECTS_DIR", tmp_path / "claude-projects")
    (tmp_path / "claude" / "projects").mkdir(parents=True)
    if spans is not None:
        (tmp_path / "grange-history.json").write_text(json.dumps({"spans": spans}))

    async def go():
        app = main.AgentFarm(settings=settings or Settings())
        app.collector.snapshot = lambda: list(sessions)
        app.expiry = ExpiryScanner(tmp_path / "claude")
        app.limits.refresh = lambda force=False: False
        async with app.run_test(size=(140, 46)) as pilot:
            await pilot.pause(1.0)
            await steps(app, pilot)
            await pilot.press("q")
    asyncio.run(go())


def test_with_nothing_reported_in_the_last_day_there_is_no_band(tmp_path, monkeypatch):
    old = time.time() - 30 * 3600

    async def steps(app, pilot):
        await pilot.press("u")
        await pilot.pause(0.3)
        assert app.query_one("#farm").display and not app.query_one("#grange-chart").display

    _run(tmp_path, monkeypatch, steps, spans=[[old, old + 600, "accepted"]])


def test_reports_in_the_last_day_show_as_a_band_under_agents_at_work(tmp_path, monkeypatch):
    now = time.time()

    async def steps(app, pilot):
        await pilot.press("u")
        await pilot.pause(0.3)
        band, farm = app.query_one("#grange-chart"), app.query_one("#farm")
        assert band.display and band.region.y == farm.region.bottom
        assert band.region.height == main.GRANGE_ROWS
        drawn = str(band.render()).splitlines()
        assert drawn[0].startswith("reports to agent-grange") and "●" in drawn[1]
        assert drawn[1].endswith("now  off")
        assert band.tooltip == main.GRANGE_TIP
        scroll = app.query_one("#usage-scroll")              # the usage graph still meets the
        assert scroll.region.y == app.query_one("#expiring").region.y >= band.region.bottom   # panes on the left

    _run(tmp_path, monkeypatch, steps, spans=[[now - 3 * 3600, now - 3600, "accepted"]])


def test_reporting_draws_its_band_and_turning_it_off_ends_the_span(tmp_path, monkeypatch, grange_pushes):
    settings = Settings(grange_report=True, grange_owner="octo-cat", grange_patterns=PROJ)
    sessions = [Instance(pid=1, session_id="a", name="proj-1 docs", status="busy")]

    async def steps(app, pilot):
        await pilot.press("u")
        await pilot.pause(0.3)
        band = app.query_one("#grange-chart")
        assert band.display and str(band.render()).splitlines()[1].endswith("now  1 session")
        assert [sp[2] for sp in app.grange_history.spans] == ["accepted"]
        await pilot.press("g")
        await pilot.pause(0.3)
        assert grange_pushes[-1][1] == []                    # the empty list that turning off sends
        assert len(app.grange_history.spans) == 1           # is not reporting
        assert app.grange_history.spans[0][1] <= time.time()
        assert str(band.render()).splitlines()[1].endswith("now  off") and band.display
        saved = json.loads((tmp_path / "grange-history.json").read_text())["spans"]
        assert [sp[2] for sp in saved] == ["accepted"]
        assert "proj" not in (tmp_path / "grange-history.json").read_text()

    _run(tmp_path, monkeypatch, steps, settings=settings, sessions=sessions)
