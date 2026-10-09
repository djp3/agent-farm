"""The supplement that finds `claude` processes missing from the registry must survive
the macOS case where reading a process's command line raises a bare PermissionError."""
import collector
from collector import Collector


class FakeProc:
    def __init__(self, pid, name, cmdline=None, error=None):
        self.info = {"pid": pid, "name": name}
        self._cmdline = cmdline or []
        self._error = error

    def cmdline(self):
        if self._error:
            raise self._error
        return self._cmdline

    def create_time(self):
        return 1000.0

    def cwd(self):
        return "/tmp/work"


def test_permission_error_on_one_process_keeps_the_others(monkeypatch):
    procs = [
        FakeProc(1, "launchd"),
        FakeProc(2, "claude", error=PermissionError("force permission denied (KERN_PROCARGS2)")),
        FakeProc(3, "claude", cmdline=["claude", "-n", "payable"]),
        FakeProc(4, "claude", cmdline=["claude", "-n"]),          # dangling -n: no name, no crash
    ]
    monkeypatch.setattr(collector.psutil, "process_iter", lambda attrs: iter(procs))
    found = Collector._unregistered(known=set())
    assert [i.pid for i in found] == [3, 4]
    assert found[0].name == "payable" and found[0].kind == "unregistered"
    assert found[1].name == ""


def test_known_pids_and_other_names_are_skipped(monkeypatch):
    procs = [FakeProc(7, "claude", cmdline=["claude"]), FakeProc(8, "claude", cmdline=["claude"])]
    monkeypatch.setattr(collector.psutil, "process_iter", lambda attrs: iter(procs))
    assert [i.pid for i in Collector._unregistered(known={7})] == [8]


def test_iterator_failure_loses_only_the_supplement(monkeypatch):
    def broken(attrs):
        raise PermissionError("sysctl denied")
    monkeypatch.setattr(collector.psutil, "process_iter", broken)
    assert Collector._unregistered(known=set()) == []
