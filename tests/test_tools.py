"""Helper programs are run by absolute path. git is resolved once: Homebrew's build via
`brew --prefix`, else Apple's when the developer tools are installed, else none, in which
case nothing is ever run (Apple's stub would pop up the installer dialog)."""
import os
import subprocess

import collector


def _fake_run(answers):
    """subprocess.run stand-in: answers maps argv[0] (or the full argv tuple) to stdout."""
    calls = []

    def run(cmd, **kw):
        calls.append(cmd)
        out = answers.get(tuple(cmd), answers.get(cmd[0]))
        if out is None:
            return subprocess.CompletedProcess(cmd, 1, "", "no")
        return subprocess.CompletedProcess(cmd, 0, out, "")
    return run, calls


def test_homebrew_git_preferred(monkeypatch):
    run, calls = _fake_run({"/opt/homebrew/bin/brew": "/opt/homebrew\n"})
    monkeypatch.setattr(collector.subprocess, "run", run)
    monkeypatch.setattr(os.path, "exists", lambda p: p == "/opt/homebrew/bin/brew")
    monkeypatch.setattr(os, "access", lambda p, mode: p == "/opt/homebrew/bin/git")
    assert collector.find_git() == "/opt/homebrew/bin/git"
    assert calls == [["/opt/homebrew/bin/brew", "--prefix"]]      # xcode-select never consulted


def test_apple_git_when_tools_installed_and_no_brew(monkeypatch):
    run, calls = _fake_run({"/usr/bin/xcode-select": "/Library/Developer/CommandLineTools\n"})
    monkeypatch.setattr(collector.subprocess, "run", run)
    monkeypatch.setattr(os.path, "exists", lambda p: False)
    monkeypatch.setattr(os, "access", lambda p, mode: p == "/usr/bin/git")
    assert collector.find_git() == "/usr/bin/git"


def test_no_git_when_tools_missing(monkeypatch):
    run, calls = _fake_run({})                                     # xcode-select -p fails
    monkeypatch.setattr(collector.subprocess, "run", run)
    monkeypatch.setattr(os.path, "exists", lambda p: False)
    monkeypatch.setattr(os, "access", lambda p, mode: True)       # the stub exists, but is unusable
    assert collector.find_git() == ""


def test_git_info_runs_nothing_without_git(monkeypatch, tmp_path):
    monkeypatch.setattr(collector, "_git_path", "")
    monkeypatch.setattr(collector, "_git_cache", {})

    def must_not_run(*a, **k):
        raise AssertionError("no git command may run when git is absent")
    monkeypatch.setattr(collector.subprocess, "run", must_not_run)
    info = collector.git_info(str(tmp_path))
    assert info == {"error": "no git"}
    assert "xcode-select --install" in collector.GIT_INSTALL_HINT


def test_ps_and_open_are_absolute(monkeypatch):
    run, calls = _fake_run({"/bin/ps": "1 ttys001\n", "/usr/bin/open": ""})
    monkeypatch.setattr(collector.subprocess, "run", run)
    assert collector.Collector._ttys_for([1]) == {1: "/dev/ttys001"}
    m = collector.ITerm2Map()
    assert m.activate("com.example.app") == (True, "")
    assert all(cmd[0].startswith("/") for cmd in calls), calls
