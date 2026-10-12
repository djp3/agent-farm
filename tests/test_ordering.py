"""The orderings decide what sits at the top of the tree. The attention ordering puts
what needs the user first; every ordering is deterministic for ties."""
from collector import Instance, TranscriptState
from ordering import get_ordering, ORDERINGS, DEFAULT_ORDERING


def _inst(pid, status, since=None, started=None, prompt_ts=None, alive=True, name="", cwd=""):
    i = Instance(pid=pid, name=name or f"s{pid}", cwd=cwd, alive=alive, status=status,
                 status_since=since, started_at=started)
    if prompt_ts is not None:
        i.transcript = TranscriptState(path=None)
        i.transcript.last_prompt_ts = prompt_ts
    return i


def test_attention_tiers_and_their_inner_orders():
    rows = [
        _inst(1, "idle", since=100),
        _inst(2, "waiting", since=50),           # waited longer than 3: first
        _inst(3, "waiting", since=80),
        _inst(4, "busy", since=10, prompt_ts=500),   # newest input: before 5
        _inst(5, "busy", since=10, prompt_ts=400),
        _inst(6, "idle", since=200),             # went idle more recently than 1
        _inst(7, "dead", started=900, alive=False),
        _inst(8, "unknown", started=950),
    ]
    got = [i.pid for i in get_ordering("attention").sort(rows)]
    assert got == [2, 3, 4, 5, 6, 1, 8, 7]


def test_name_ordering_is_alphabetical_then_project():
    rows = [_inst(1, "idle", name="beta"), _inst(2, "idle", name="Alpha"),
            _inst(3, "idle", name="alpha", cwd="/x/aaa"), _inst(4, "idle", name="alpha", cwd="/x/zzz")]
    got = [i.pid for i in get_ordering("name").sort(rows)]
    assert got in ([2, 3, 4, 1], [3, 2, 4, 1])          # case-insensitive; pid breaks exact ties
    assert got.index(3) < got.index(4)


def test_started_ordering_is_launch_order_with_inactive_last():
    rows = [_inst(1, "idle", started=300), _inst(2, "busy", started=100),
            _inst(3, "dead", started=50, alive=False)]
    assert [i.pid for i in get_ordering("started").sort(rows)] == [2, 1, 3]


def test_every_ordering_is_stable_for_ties_and_unknown_name_fails():
    rows = [_inst(9, "idle"), _inst(7, "idle"), _inst(8, "idle")]
    for o in ORDERINGS:
        assert [i.pid for i in o.sort(rows)] == [7, 8, 9]
    assert get_ordering(DEFAULT_ORDERING).name == "attention"
    try:
        get_ordering("nope")
    except KeyError:
        pass
    else:
        raise AssertionError("unknown ordering must raise")
