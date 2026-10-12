"""The once-a-second collector tick, the usage scan and the iTerm2 tab-map refresh each
run on a thread. Textual only flags a cancelled thread worker, it does not stop it, so
each of them declines to run while its previous run is still going."""
import threading

import collector
from collector import Collector, ITerm2Map
from usage import UsageScanner


def test_snapshot_declines_while_previous_tick_runs():
    c = Collector()
    assert c._busy.acquire(blocking=False)          # pretend a tick is in flight
    try:
        assert c.snapshot() is None
    finally:
        c._busy.release()
    assert c._busy.acquire(blocking=False)          # released cleanly for the next tick
    c._busy.release()


def test_usage_scan_declines_while_previous_scan_runs():
    u = UsageScanner()
    assert u._busy.acquire(blocking=False)
    try:
        assert u.scan() is False
    finally:
        u._busy.release()


def test_iterm_refresh_declines_while_another_refresh_runs(monkeypatch):
    m = ITerm2Map()
    m.available = True

    def must_not_run(*args, **kwargs):
        raise AssertionError("it2 must not be invoked while a refresh holds the lock")

    monkeypatch.setattr(collector.subprocess, "run", must_not_run)
    assert m._lock.acquire(blocking=False)
    try:
        m.refresh(force=True)                       # returns without touching it2
    finally:
        m._lock.release()


def test_iterm_background_thread_starts_once(monkeypatch):
    m = ITerm2Map()
    m.available = True
    monkeypatch.setattr(m, "_loop", lambda: threading.Event().wait(0.01))
    m.start_background()
    first = m._thread
    m.start_background()
    assert m._thread is first
