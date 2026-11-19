"""
What agent-farm reports to agent-grange, the relay that carries each machine's session
status to the team's boards. agent-grange passes on whatever it is given and leaves the
choice of sessions to agent-farm, so the choice lives here: a list of regular expressions
in the settings, each on or off. A session is reported when its name matches a pattern
that is on. Matching is re.search, so a pattern matches anywhere in the name unless it is
anchored with ^ or $.

samples() draws a few strings a pattern matches, for the settings panel to show beside it.
"""
from __future__ import annotations

import random
import re
import string
from functools import lru_cache

from re import _constants as sre      # the parse tree re builds; stable in practice, and
from re import _parser as sre_parse   # samples() gives up quietly if it ever changes

PATTERN_MAX = 200       # characters in one pattern
SAMPLE_COUNT = 4        # samples shown for a pattern
SAMPLE_MAX = 40         # characters in one sample; a longer draw is dropped
REPEAT_EXTRA = 3        # a repeat draws at most this many past its minimum

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
    except re.error as e:
        return str(e)               # "missing ), unterminated subpattern at position 0"
    return None


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


def reported(name: str, entries: list[dict]) -> bool:
    """Whether a session with this name goes to agent-grange: a pattern that is on matches it."""
    for e in entries:
        rx = _compiled(e["pattern"]) if e["enabled"] else None
        if rx is not None and rx.search(name):
            return True
    return False


class _GiveUp(Exception):
    """This draw can't make a usable sample."""


def samples(pattern: str, count: int = SAMPLE_COUNT) -> list[str]:
    """A few different strings the pattern matches, drawn from the pattern itself: readable
    letters and digits where it allows a choice, and short repeats. The same pattern always
    gets the same samples. Empty for an invalid pattern, or when no draw works (a pattern
    that needs a long string, or lookarounds that no draw satisfies)."""
    rx = _compiled(pattern)
    if rx is None:
        return []
    try:
        tree = sre_parse.parse(pattern)
    except Exception:
        return []
    rng = random.Random(pattern)
    out: list[str] = []
    for _ in range(count * 10):
        try:
            s = _draw(tree, rng, {})
        except (_GiveUp, RecursionError):
            continue
        except Exception:               # a parse tree this code doesn't know
            break
        if s not in out and rx.search(s):
            out.append(s)
            if len(out) == count:
                break
    return out


def _draw(items, rng: random.Random, groups: dict) -> str:
    out = ""
    for op, av in items:
        out += _one(op, av, rng, groups)
        if len(out) > SAMPLE_MAX:
            raise _GiveUp
    return out


def _one(op, av, rng: random.Random, groups: dict) -> str:
    if op == sre.LITERAL:
        return chr(av)
    if op == sre.NOT_LITERAL:
        return rng.choice([c for c in _POOL if ord(c) != av])
    if op == sre.ANY:
        return rng.choice(_ANY)
    if op == sre.IN:
        return _from_class(av, rng)
    if op == sre.BRANCH:
        return _draw(rng.choice(av[1]), rng, groups)
    if op == sre.SUBPATTERN:
        group, _add, _del, sub = av
        s = _draw(sub, rng, groups)
        if group is not None:
            groups[group] = s
        return s
    if op in _REPEATS:
        lo, hi, sub = av
        if lo > SAMPLE_MAX:
            raise _GiveUp
        return "".join(_draw(sub, rng, groups) for _ in range(rng.randint(lo, min(hi, lo + REPEAT_EXTRA))))
    if op == getattr(sre, "ATOMIC_GROUP", None):
        return _draw(av, rng, groups)
    if op == sre.GROUPREF:
        return groups.get(av, "")       # an unset group fails the match; the check drops it
    if op == sre.GROUPREF_EXISTS:
        group, yes, no = av
        return _draw(yes if group in groups else (no or []), rng, groups)
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
