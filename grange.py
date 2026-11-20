"""
What agent-farm reports to agent-grange, the relay that carries each machine's session
status to the team's boards. agent-grange passes on whatever it is given and leaves the
choice of sessions to agent-farm, so the choice lives here: a list of regular expressions
in the settings, each on or off. A session is reported when its name matches a pattern
that is on. Matching is re.search, so a pattern matches anywhere in the name unless it is
anchored with ^ or $.

samples() draws a few strings a pattern matches, for the settings panel to show beside it.

The push (agent-grange's docs/requirements.md, 4.1 and 5.1): PUT http://127.0.0.1:<port>/v1/farm
with {"agents": [{"owner", "name", "state"}, ...]}, the whole list each time, on every change
and at least every 30 seconds; after 60 seconds without one the grange marks this farm stale
on every teammate's board. Only those three strings leave the machine. The owner is the
user's GitHub login, the name and state are the session's as the monitor shows them.
report() picks the entries on the UI thread (reported() needs its timer there), Reporter
decides when a push is due, and put_farm() sends it from a worker thread.

Python's re has no time limit, and some patterns backtrack catastrophically: (a+)+b on a
long run of a's tries every way to split it. So every search here runs under a timer
(_time_limit): a pattern's examples get TIME_LIMIT_S in all, and so does each name it is
checked against. re's matching loop checks for signals, so the timer stops a runaway
search; a pattern that runs out of time has no examples, and counts as not matching.
"""
from __future__ import annotations

import http.client
import json
import logging
import os
import random
import re
import signal
import string
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from re import _constants as sre      # the parse tree re builds; stable in practice, and
from re import _parser as sre_parse   # samples() gives up quietly if it ever changes

PATTERN_MAX = 200       # characters in one pattern
SAMPLE_COUNT = 4        # samples shown for a pattern
SAMPLE_MAX = 40         # characters in one sample; a longer draw is dropped
REPEAT_EXTRA = 3        # a repeat draws at most this many past its minimum
TIME_LIMIT_S = 0.25     # for one pattern's examples, and for checking one name against it
DRAW_STEPS = 5000       # pieces of a pattern the drawer may visit, across all of its draws

HOST = "127.0.0.1"      # the grange listens on loopback only
DEFAULT_PORT = 7788     # GRANGE_PORT in the grange's .env
HEARTBEAT_S = 20.0      # the grange asks for a push at least every 30 s and calls 60 s stale
TIMEOUT_S = 3.0         # for one push: connecting, sending and the answer
FIELD_MAX = 256         # bytes in each string the grange accepts
AGENTS_MAX = 200        # entries in one push
REPLY_MAX = 64 * 1024   # bytes of an answer read, for its {"error": ...}
# A GitHub login: letters, digits and single hyphens, not at either end, at most 39 characters.
LOGIN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}")

log = logging.getLogger("agent_farm.grange")

_ANY = string.ascii_lowercase + string.digits                   # what . draws from
_POOL = _ANY + string.ascii_uppercase + "-_ ."                  # a class draws from these first
_REPEATS = {sre.MAX_REPEAT, sre.MIN_REPEAT, getattr(sre, "POSSESSIVE_REPEAT", sre.MAX_REPEAT)}
_EMPTY = {sre.AT, sre.ASSERT, sre.ASSERT_NOT}                   # take no characters


def check(pattern: str) -> str | None:
    """Why a pattern can't be used, or None when it can."""
    if not pattern.strip():
        return "empty"
    if len(pattern) > PATTERN_MAX:
        return f"longer than {PATTERN_MAX} characters"
    try:
        re.compile(pattern)
    except Exception as e:          # usually re.error ("missing ), unterminated subpattern at
        return str(e)               # position 0"); OverflowError for a{9999999999}; whatever it
    return None                     # is, the pattern can't be used and typing it mustn't crash


def clean(entries: object) -> list[dict]:
    """The settings file's list as {"pattern": str, "enabled": bool} entries. Anything else
    is dropped. A pattern that doesn't compile is kept, so a hand edit survives the next
    save, but it never matches. An entry without "enabled" is on."""
    out = []
    for e in entries if isinstance(entries, list) else []:
        if isinstance(e, dict) and isinstance(e.get("pattern"), str) and e["pattern"]:
            enabled = e.get("enabled", True)
            out.append({"pattern": e["pattern"], "enabled": enabled if isinstance(enabled, bool) else True})
    return out


@lru_cache(maxsize=256)
def _compiled(pattern: str) -> re.Pattern | None:
    return None if check(pattern) else re.compile(pattern)


def _on_main_thread() -> bool:
    return threading.current_thread() is threading.main_thread()


class _TooSlow(BaseException):
    """The time limit ran out. A BaseException, so no `except Exception` on the way swallows it."""


@contextmanager
def _time_limit(seconds: float):
    """Raise _TooSlow in the block once `seconds` have passed. The timer is SIGALRM, which
    only the main thread receives (the UI runs there); on any other thread the block can't
    be bounded, so _TooSlow comes at once and the caller takes its safe answer. Not nestable."""
    if not _on_main_thread():
        raise _TooSlow
    armed = True

    def on_alarm(_signum, _frame):
        if armed:                       # a signal that lands after the block has ended is ignored
            raise _TooSlow

    previous = signal.signal(signal.SIGALRM, on_alarm)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        armed = False
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous if previous is not None else signal.SIG_DFL)


_slow_warned: set[str] = set()


def reported(name: str, entries: list[dict]) -> bool:
    """Whether a session with this name goes to agent-grange: a pattern that is on matches it.
    A pattern that takes longer than TIME_LIMIT_S on the name counts as not matching, so a
    slow pattern never reports a session by accident (it is logged once, without the name)."""
    for e in entries:
        rx = _compiled(e["pattern"]) if e["enabled"] else None
        if rx is None:
            continue
        try:
            with _time_limit(TIME_LIMIT_S):
                hit = rx.search(name) is not None
        except _TooSlow:
            if e["pattern"] not in _slow_warned:
                _slow_warned.add(e["pattern"])
                log.warning("agent-grange pattern %r took over %.2fs on a session name; "
                            "counted as not matching", e["pattern"], TIME_LIMIT_S)
            continue
        if hit:
            return True
    return False


# ---- the push --------------------------------------------------------------------

def gh_login() -> str:
    """The GitHub login gh is signed in with on github.com, or "" when there is none. Only
    the `user:` line under github.com in gh's hosts.yml is read: the file can hold a token
    too, and no other line is kept or logged."""
    base = os.environ.get("GH_CONFIG_DIR") or \
        Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "gh"
    try:
        text = (Path(base) / "hosts.yml").read_text(errors="replace")
    except (OSError, ValueError):
        return ""
    host = ""
    for line in text.splitlines():
        if line[:1] not in ("", " ", "\t", "#"):
            host = line.split(":", 1)[0].strip().strip("\"'")
        elif host == "github.com":
            m = re.fullmatch(r"[ ]{2,4}user:\s*([\"']?)([^\"'\s]+)\1\s*", line)
            if m and LOGIN.fullmatch(m[2]):
                return m[2]
    return ""


def _fit(s: str) -> str:
    """s as valid text of at most FIELD_MAX bytes of UTF-8, cut at a character boundary. A lone
    surrogate (a name taken from bytes that weren't UTF-8) becomes "?": it can't be encoded,
    and the push would fail on every try."""
    return s.encode("utf-8", "replace")[:FIELD_MAX].decode("utf-8", "ignore")


def report(sessions, entries: list[dict], owner: str) -> tuple[list[dict], int]:
    """The push's entries: one per session whose name a pattern that is on matches, in the
    order given, each with exactly owner, name and state; and how many sessions matched.
    Past AGENTS_MAX the rest are left out, since the grange would refuse the whole list.
    A session without a name is never sent: a board finds the pull request from the name.
    `sessions` are anything with .name and .status. Call it on the main thread (reported())."""
    picked = [s for s in sessions if s.name and reported(s.name, entries)]
    agents = [{"owner": _fit(owner), "name": _fit(s.name), "state": _fit(s.status)}
              for s in picked[:AGENTS_MAX]]
    return agents, len(picked)


@dataclass(frozen=True)
class Reply:
    """How one push went: the HTTP status, or None when nothing answered, and what went wrong
    in a few words ("" when the grange took the list)."""
    code: int | None
    problem: str = ""

    @property
    def kind(self) -> str:
        """The reply's sort, for the status line; the log notes only a change of kind."""
        if self.code == 204:
            return "accepted"
        if self.code == 503:
            return "out of date"
        if self.code is None:
            return "not running" if self.problem == "not running" else "no answer"
        return "refused"


def _error_in(data: bytes) -> str:
    """The "error" of a {"error": "..."} answer, made safe to show on one line."""
    try:
        e = json.loads(data).get("error")
    except Exception:
        return ""
    return " ".join(e.split())[:200] if isinstance(e, str) else ""


def put_farm(port: int, agents: list[dict]) -> Reply:
    """Send the list to the grange on this Mac and say how it went. Never raises: a grange that
    isn't running, is out of date or refuses the list is an answer like any other, and the
    next push goes on schedule. http.client rather than urllib, so no proxy setting can route
    it anywhere else; a bytes body always goes with Content-Length, never chunked."""
    body = json.dumps({"agents": agents}, ensure_ascii=False, separators=(",", ":")).encode()
    conn = http.client.HTTPConnection(HOST, port, timeout=TIMEOUT_S)
    try:
        conn.request("PUT", "/v1/farm", body=body,
                     headers={"Content-Type": "application/json", "Connection": "close"})
        r = conn.getresponse()
        data = r.read(REPLY_MAX)
    except ConnectionRefusedError:
        return Reply(None, "not running")
    except TimeoutError:
        return Reply(None, f"no answer in {TIMEOUT_S:g} s")
    except (OSError, http.client.HTTPException, ValueError) as e:   # ValueError: a port out of range
        return Reply(None, getattr(e, "strerror", None) or type(e).__name__)
    finally:
        conn.close()
    if r.status == 204:
        return Reply(204)
    return Reply(r.status, _error_in(data) or r.reason or "")


class Reporter:
    """When to push, decided on the UI thread. Give next_push() each snapshot's entries (None
    while reporting is off): it returns the list to send now, or None. Give done() the answer.

    One push is out at a time, so they reach the grange in the order they were made. A push
    is due when the list changed in any way but order (the grange ignores order), or
    HEARTBEAT_S after the last one, answered or not: a grange that is down is tried again on
    that schedule. The heartbeat rides on the snapshots, so a monitor that stops taking them
    stops pushing, and the grange marks it stale as it should. Turning reporting off sends
    one empty list, so the boards drop these sessions at once instead of keeping them
    flagged stale."""

    def __init__(self) -> None:
        self.sent: tuple | None = None      # the last list sent, as _key(); None while off
        self.last_try = float("-inf")
        self.out = False                    # a push is on its way
        self.reply: Reply | None = None     # the last answer, and when it came
        self.reply_at = 0.0
        self.count = 0                      # entries in the last push
        self.matched = 0                    # sessions that matched for it (more past AGENTS_MAX)

    @staticmethod
    def _key(agents: list[dict]) -> tuple:
        return tuple(sorted((a["owner"], a["name"], a["state"]) for a in agents))

    def next_push(self, agents: list[dict] | None, now: float, matched: int = 0) -> list[dict] | None:
        if self.out:
            return None
        if agents is None:                  # off
            if not self.sent:               # nothing of ours on the boards
                self.sent = None
                return None
            self.sent, agents, matched = None, [], 0
        else:
            key = self._key(agents)
            if key == self.sent and now - self.last_try < HEARTBEAT_S:
                return None
            if self.sent is None:           # just turned on: an answer from before says nothing now
                self.reply = None
            self.sent = key
        self.out, self.last_try = True, now
        self.count, self.matched = len(agents), max(matched, len(agents))
        return agents

    def done(self, reply: Reply, now: float, port: int) -> None:
        self.out = False
        if self.reply is None or reply.kind != self.reply.kind:
            if reply.kind == "accepted":
                log.info("agent-grange on port %d took the report", port)
            elif reply.kind == "not running":
                log.info("no agent-grange on port %d; trying every %g s", port, HEARTBEAT_S)
            elif reply.kind == "out of date":
                log.warning("agent-grange on port %d is out of date (503) until make update", port)
            else:
                log.warning("agent-grange on port %d: %s %s", port, reply.code or "", reply.problem)
        self.reply, self.reply_at = reply, now

    def words(self, on: bool, owner: str, port: int, now: float) -> str:
        """The state of reporting in a sentence, for the settings panel and the fleet tooltip."""
        if not on:
            return "Off: nothing is sent."
        if not owner:
            return "On, but nothing is sent until there is a GitHub login to send as the owner."
        r, again = self.reply, f"Trying again every {HEARTBEAT_S:g} s."
        if r is None:
            return f"On: sending to port {port}…"
        if r.kind == "accepted":
            age = now - self.reply_at
            s = (f"On: {self.count} session{'' if self.count == 1 else 's'} sent to port {port}, "
                 f"accepted {'just now' if age < 2 else f'{age:.0f} s ago'}.")
            if self.matched > self.count:
                s += f" {self.matched - self.count} more match, past the grange's limit of {AGENTS_MAX}."
            return s
        if r.kind == "not running":
            return f"On, but no agent-grange is running on port {port} (make up starts it). {again}"
        if r.kind == "out of date":
            return f"On, but the grange takes nothing until make update brings it up to date. {again}"
        if r.kind == "refused":
            return f"On, but the grange refused the list ({r.code}: {r.problem}). {again}"
        return f"On, but the push to port {port} failed ({r.problem}). {again}"


class _GiveUp(Exception):
    """This draw can't make a usable sample."""


class _OutOfSteps(Exception):
    """The pattern's draws used up DRAW_STEPS: stop drawing, keep what was found."""


class _Steps:
    """The step budget shared by all of one pattern's draws. Output length alone doesn't
    bound the work: (?:(?:^){40}){40}... visits billions of anchors without a character."""

    def __init__(self, n: int) -> None:
        self.left = n

    def take(self) -> None:
        self.left -= 1
        if self.left < 0:
            raise _OutOfSteps


def samples(pattern: str, count: int = SAMPLE_COUNT) -> list[str] | None:
    """A few different strings the pattern matches, drawn from the pattern itself: readable
    letters and digits where it allows a choice, and short repeats. The same pattern always
    gets the same samples. Empty for an invalid pattern, or when no draw works (a pattern
    that needs a long string, or lookarounds that no draw satisfies). None when drawing and
    checking them took longer than TIME_LIMIT_S: the pattern backtracks badly enough that it
    would be slow on names too, or when called off the main thread, where it can't be timed.
    Worked out once per pattern; the panel redraws often."""
    if not _on_main_thread():
        return None                     # not cached: the main thread may well manage it
    found = _samples(pattern, count)
    return None if found is None else list(found)


@lru_cache(maxsize=256)
def _samples(pattern: str, count: int) -> tuple[str, ...] | None:
    rx = _compiled(pattern)
    if rx is None:
        return ()
    try:
        with _time_limit(TIME_LIMIT_S):
            return tuple(_draw_samples(pattern, rx, count))
    except _TooSlow:
        return None


def _draw_samples(pattern: str, rx: re.Pattern, count: int) -> list[str]:
    try:
        tree = sre_parse.parse(pattern)
    except Exception:
        return []
    rng, steps = random.Random(pattern), _Steps(DRAW_STEPS)
    out: list[str] = []
    for _ in range(count * 10):
        try:
            s = _draw(tree, rng, {}, steps)
        except (_GiveUp, RecursionError):
            continue
        except Exception:               # out of steps, or a parse tree this code doesn't know
            break
        if s not in out and rx.search(s):
            out.append(s)
            if len(out) == count:
                break
    return out


def _draw(items, rng: random.Random, groups: dict, steps: _Steps) -> str:
    steps.take()
    out = ""
    for op, av in items:
        out += _one(op, av, rng, groups, steps)
        if len(out) > SAMPLE_MAX:
            raise _GiveUp
    return out


def _one(op, av, rng: random.Random, groups: dict, steps: _Steps) -> str:
    steps.take()
    if op == sre.LITERAL:
        return chr(av)
    if op == sre.NOT_LITERAL:
        return rng.choice([c for c in _POOL if ord(c) != av])
    if op == sre.ANY:
        return rng.choice(_ANY)
    if op == sre.IN:
        return _from_class(av, rng)
    if op == sre.BRANCH:
        return _draw(rng.choice(av[1]), rng, groups, steps)
    if op == sre.SUBPATTERN:
        group, _add, _del, sub = av
        s = _draw(sub, rng, groups, steps)
        if group is not None:
            groups[group] = s
        return s
    if op in _REPEATS:
        lo, hi, sub = av
        if lo > SAMPLE_MAX:
            raise _GiveUp
        return "".join(_draw(sub, rng, groups, steps) for _ in range(rng.randint(lo, min(hi, lo + REPEAT_EXTRA))))
    if op == getattr(sre, "ATOMIC_GROUP", None):
        return _draw(av, rng, groups, steps)
    if op == sre.GROUPREF:
        return groups.get(av, "")       # an unset group fails the match; the check drops it
    if op == sre.GROUPREF_EXISTS:
        group, yes, no = av
        return _draw(yes if group in groups else (no or []), rng, groups, steps)
    if op in _EMPTY:
        return ""                       # a draw that breaks an anchor or lookaround is dropped
    raise _GiveUp                       # FAILURE, or something newer


def _from_class(items, rng: random.Random) -> str:
    if items and items[0][0] == sre.NEGATE:
        allowed = [c for c in _POOL if not _in_class(c, items[1:])]
    else:
        allowed = [c for c in _POOL if _in_class(c, items)] or _class_chars(items)
    if not allowed:
        raise _GiveUp
    return rng.choice(allowed)


def _in_class(c: str, items) -> bool:
    for op, av in items:
        if op == sre.LITERAL and ord(c) == av:
            return True
        if op == sre.RANGE and av[0] <= ord(c) <= av[1]:
            return True
        if op == sre.CATEGORY and _in_category(c, getattr(av, "name", "")):
            return True
    return False


def _in_category(c: str, name: str) -> bool:
    if "DIGIT" in name:
        hit = c.isdigit()
    elif "SPACE" in name:
        hit = c.isspace()
    elif "WORD" in name:
        hit = c.isalnum() or c == "_"
    elif "LINEBREAK" in name:
        hit = c == "\n"
    else:
        return False
    return hit != ("_NOT_" in name)


def _class_chars(items) -> list[str]:
    """A class with nothing in the readable pool ([é], [\\u4e00-\\u9fff]): its own characters."""
    out = []
    for op, av in items:
        if op == sre.LITERAL:
            out.append(chr(av))
        elif op == sre.RANGE:
            out.extend(chr(av[0] + k) for k in range(min(av[1] - av[0] + 1, 26)))
    return out
