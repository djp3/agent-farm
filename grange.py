"""
What agent-farm reports to agent-grange, the relay that carries each machine's session
status to the team's boards. agent-grange passes on whatever it is given and leaves the
choice of sessions to agent-farm, so the choice lives here: a list of regular expressions
in the settings, each on or off. A session is reported when its name matches a pattern
that is on. Matching is re.search, so a pattern matches anywhere in the name unless it is
anchored with ^ or $.

samples() draws a few strings a pattern matches, for the settings panel to show beside it.

Python's re has no time limit, and some patterns backtrack catastrophically: (a+)+b on a
long run of a's tries every way to split it. So every search here runs under a timer
(_time_limit): a pattern's examples get TIME_LIMIT_S in all, and so does each name it is
checked against. re's matching loop checks for signals, so the timer stops a runaway
search; a pattern that runs out of time has no examples, and counts as not matching.
"""
from __future__ import annotations

import logging
import random
import re
import signal
import string
import threading
from contextlib import contextmanager
from functools import lru_cache

from re import _constants as sre      # the parse tree re builds; stable in practice, and
from re import _parser as sre_parse   # samples() gives up quietly if it ever changes

PATTERN_MAX = 200       # characters in one pattern
SAMPLE_COUNT = 4        # samples shown for a pattern
SAMPLE_MAX = 40         # characters in one sample; a longer draw is dropped
REPEAT_EXTRA = 3        # a repeat draws at most this many past its minimum
TIME_LIMIT_S = 0.25     # for one pattern's examples, and for checking one name against it
DRAW_STEPS = 5000       # pieces of a pattern the drawer may visit, across all of its draws

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
