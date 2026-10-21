"""
Orderings for the instance list.

An Ordering is a sequence of Tiers. An instance belongs to the first tier whose
`match` accepts it and is sorted within that tier by `key` (ascending). Instances
that match no tier go last. Add a new ordering by appending to ORDERINGS; the TUI
offers them in Settings (s) and accepts `--order NAME` on the command line.

Keys must be comparable within a tier; the helpers below turn optional timestamps
into floats so None never breaks a sort.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Callable, Optional

from collector import Instance

INF = math.inf


# ---- key helpers ------------------------------------------------------------ #
def newest_first(ts: Optional[float]) -> float:
    """Larger timestamp sorts first; missing timestamps sink to the bottom."""
    return -(ts or 0.0)


def oldest_first(ts: Optional[float]) -> float:
    return ts if ts is not None else INF


def last_input_ts(i: Instance) -> Optional[float]:
    """When the user last gave this instance a prompt (falls back to its status change)."""
    t = i.transcript
    if t and t.last_prompt_ts:
        return t.last_prompt_ts
    return i.status_since


def _minute(ts: Optional[float]) -> Optional[float]:
    return None if ts is None else float(int(ts // 60) * 60)


def last_event_ts(i: Instance) -> Optional[float]:
    t = i.transcript
    return (t.last_ts if t else None) or i.status_since or i.started_at


# ---- model ------------------------------------------------------------------ #
@dataclass(frozen=True)
class Tier:
    name: str                                   # short label, for readers of this file
    match: Callable[[Instance], bool]
    key: Callable[[Instance], Any]              # ascending within the tier


@dataclass(frozen=True)
class Ordering:
    name: str
    description: str
    tiers: tuple[Tier, ...]

    def tier_index(self, i: Instance) -> int:
        for idx, t in enumerate(self.tiers):
            try:
                if t.match(i):
                    return idx
            except Exception:
                continue
        return len(self.tiers)

    def sort_key(self, i: Instance) -> tuple:
        idx = self.tier_index(i)
        if idx < len(self.tiers):
            try:
                return (idx, self.tiers[idx].key(i), i.pid)
            except Exception:
                return (idx, INF, i.pid)
        return (idx, 0, i.pid)

    def sort(self, instances: list[Instance]) -> list[Instance]:
        return sorted(instances, key=self.sort_key)


# ---- predicates ------------------------------------------------------------- #
def is_waiting(i: Instance) -> bool:
    return i.alive and i.status == "waiting"


def is_busy(i: Instance) -> bool:
    return i.alive and i.status == "busy"


def is_shell(i: Instance) -> bool:
    return i.alive and i.status == "shell"


def is_idle(i: Instance) -> bool:
    return i.alive and i.status == "idle"


def is_inactive(i: Instance) -> bool:
    return (not i.alive) or i.status in ("dead", "unknown")


def always(i: Instance) -> bool:
    return True


# ---- the orderings ---------------------------------------------------------- #
ATTENTION = Ordering(
    name="attention",
    description="what needs me first: waiting → working on my latest input → running a shell → "
                "recently idle → inactive",
    tiers=(
        Tier("waiting", is_waiting, lambda i: oldest_first(i.status_since)),       # longest wait first
        Tier("working", is_busy, lambda i: newest_first(last_input_ts(i))),      # my latest input first
        Tier("shell", is_shell, lambda i: newest_first(last_input_ts(i))),       # waiting on its command
        Tier("idle", is_idle, lambda i: newest_first(i.status_since)),           # most recently idle first
        Tier("inactive", is_inactive, lambda i: newest_first(i.started_at)),     # newest first
    ),
)

ACTIVITY = Ordering(
    name="activity",
    description="most recent transcript event first, regardless of state",
    tiers=(
        # bucketed to the minute so busy instances don't swap places every tick
        Tier("live", lambda i: i.alive, lambda i: newest_first(_minute(last_event_ts(i)))),
        Tier("inactive", is_inactive, lambda i: newest_first(i.started_at)),
    ),
)

STARTED = Ordering(
    name="started",
    description="launch order, oldest session first",
    tiers=(
        Tier("live", lambda i: i.alive, lambda i: oldest_first(i.started_at)),
        Tier("inactive", is_inactive, lambda i: oldest_first(i.started_at)),
    ),
)

NAME = Ordering(
    name="name",
    description="alphabetical by session name, then project",
    tiers=(
        Tier("all", always, lambda i: ((i.name or "").lower(), i.project.lower(), i.pid)),
    ),
)

ORDERINGS: tuple[Ordering, ...] = (ATTENTION, ACTIVITY, STARTED, NAME)
DEFAULT_ORDERING = ATTENTION.name


def get_ordering(name: str) -> Ordering:
    for o in ORDERINGS:
        if o.name == name:
            return o
    raise KeyError(f"unknown ordering {name!r}; choose from {[o.name for o in ORDERINGS]}")

