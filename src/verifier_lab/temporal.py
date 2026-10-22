"""Uncertainty-aware comparisons (chapter 06). Overlap or missing data yields UNKNOWN, never a guess.

All numbers are exact Decimals. No clock, no float, no default tolerance.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum


class Tri(StrEnum):
    YES = "yes"
    NO = "no"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Interval:
    """Closed interval [lo, hi] of possible values in one declared coordinate."""
    lo: Decimal
    hi: Decimal
    coord: str = "default"

    def __post_init__(self):
        if not (self.lo.is_finite() and self.hi.is_finite()) or self.lo > self.hi:
            raise ValueError("interval needs finite lo <= hi")

    @staticmethod
    def point(v: Decimal, coord: str = "default") -> "Interval":
        return Interval(v, v, coord)

    def to_dict(self) -> dict:
        return {"lo": str(self.lo), "hi": str(self.hi), "coord": self.coord}


def _same_coord(*xs: Interval) -> bool:
    return len({x.coord for x in xs}) == 1


def definitely_before(a: Interval, b: Interval) -> Tri:
    """YES: A's latest < B's earliest. NO: A before B is impossible (B's latest < A's earliest, or both are
    the same single instant). Touching closed intervals and overlaps stay UNKNOWN."""
    if not _same_coord(a, b):
        return Tri.UNKNOWN                      # different clocks are incomparable without a mapping
    if a.hi < b.lo:
        return Tri.YES
    if b.hi < a.lo or (a.lo == a.hi == b.lo == b.hi):
        return Tri.NO
    return Tri.UNKNOWN


@dataclass(frozen=True)
class Activity:
    """Activity with uncertain half-open bounds [start, end): each bound is an Interval of possible values."""
    start: Interval
    end: Interval

    def __post_init__(self):
        if not _same_coord(self.start, self.end):
            raise ValueError("start and end must share a coordinate")
        if self.end.hi < self.start.lo:
            raise ValueError("activity cannot end before it can start")


def positive_overlap(a: Activity, b: Activity) -> Tri:
    """Half-open positive-duration overlap: max(start) < min(end).
    YES only when guaranteed under every possibility; NO only when impossible; otherwise UNKNOWN."""
    if not _same_coord(a.start, b.start):
        return Tri.UNKNOWN
    if max(a.start.hi, b.start.hi) < min(a.end.lo, b.end.lo):
        return Tri.YES
    if max(a.start.lo, b.start.lo) >= min(a.end.hi, b.end.hi):
        return Tri.NO                           # no assignment of possible values yields positive overlap
    return Tri.UNKNOWN


def values_equal(a: Interval, b: Interval) -> Tri:
    if not _same_coord(a, b):
        return Tri.UNKNOWN
    if a.lo == a.hi == b.lo == b.hi:
        return Tri.YES
    if a.hi < b.lo or b.hi < a.lo:
        return Tri.NO                           # equality impossible
    return Tri.UNKNOWN                          # overlap: both may be possible, equality unproven


@dataclass(frozen=True)
class LogicalTime:
    clock_id: str
    tick: int


def logical_before(a: LogicalTime, b: LogicalTime) -> Tri:
    """Ticks order only within one clock; different clocks are incomparable (no shared epoch is inferred)."""
    if a.clock_id != b.clock_id:
        return Tri.UNKNOWN
    return Tri.YES if a.tick < b.tick else Tri.NO


def possibility_sets_compatible(a: frozenset, b: frozenset) -> Tri:
    """Possible-value sets: disjoint => incompatible (NO); overlap => both may hold (UNKNOWN, not equality).
    An empty possibility set is invalid evidence and raises."""
    if not a or not b:
        raise ValueError("empty possibility set is invalid unless impossibility is modelled explicitly")
    if a.isdisjoint(b):
        return Tri.NO
    if len(a) == len(b) == 1 and a == b:
        return Tri.YES
    return Tri.UNKNOWN


def happens_before_reachable(edges, a: str, b: str) -> Tri:
    """Reference traversal over a happens-before DAG. No path means *unordered*, not simultaneous or reversed."""
    adj: dict[str, list[str]] = {}
    for x, y in edges:
        adj.setdefault(x, []).append(y)
    seen, stack = {a}, [a]
    while stack:
        n = stack.pop()
        for m in sorted(adj.get(n, [])):
            if m == b:
                return Tri.YES
            if m not in seen:
                seen.add(m)
                stack.append(m)
    return Tri.UNKNOWN
