"""Reference transactional key-value store (chapter 05): fictional, immutable state in / state out.

Future-affecting fields (the field inventory) are written out by hand: entries, next_identifier,
logical_tick, random_cursor, operation_cursor. Candidates must never share this transition code.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass

SEMANTICS_VERSION = "ref-store-1"
FIELDS = ("entries", "next_identifier", "logical_tick", "random_cursor", "operation_cursor")
SIMPLE_OPS = ("put", "delete", "allocate", "draw")


class OperationError(ValueError):
    """Structurally invalid operation (not a rejected operation)."""


@dataclass(frozen=True)
class StoreState:
    entries: tuple[tuple[str, str], ...] = ()
    next_identifier: int = 0
    logical_tick: int = 0
    random_cursor: int = 0
    operation_cursor: int = 0

    def to_dict(self) -> dict:
        return {"entries": [list(e) for e in self.entries], "next_identifier": self.next_identifier,
                "logical_tick": self.logical_tick, "random_cursor": self.random_cursor,
                "operation_cursor": self.operation_cursor}

    @staticmethod
    def from_dict(d: dict) -> "StoreState":
        if set(d) != set(FIELDS):
            raise ValueError(f"state fields {sorted(d)} != inventory {sorted(FIELDS)}")
        return StoreState(tuple(sorted((str(k), str(v)) for k, v in d["entries"])), int(d["next_identifier"]),
                          int(d["logical_tick"]), int(d["random_cursor"]), int(d["operation_cursor"]))


@dataclass(frozen=True)
class Transition:
    state: StoreState
    observations: tuple[str, ...]
    accepted: bool
    reason: str = ""


def validate_operation(op: dict, nested: bool = False) -> None:
    if not isinstance(op, dict) or not isinstance(op.get("op"), str):
        raise OperationError("operation needs an 'op' string")
    kind = op["op"]
    if kind == "txn":
        if nested:
            raise OperationError("nested transactions are not supported")
        members = op.get("ops")
        if not isinstance(members, list):
            raise OperationError("txn needs an 'ops' list")
        for m in members:
            validate_operation(m, nested=True)
        return
    if kind not in SIMPLE_OPS:
        raise OperationError(f"unknown operation {kind!r}")
    if not isinstance(op.get("key"), str) or not op["key"]:
        raise OperationError("operation needs a non-empty string key")
    if kind == "put" and not isinstance(op.get("value"), str):
        raise OperationError("put needs a string value")


def draw_value(seed: str, cursor: int) -> str:
    return hashlib.sha256(f"{seed}|{cursor}".encode()).hexdigest()[:8]


def _step(work: dict, op: dict, seed: str):
    """Apply one simple op to the mutable working dict; returns (accepted, observation|reason)."""
    kind, key = op["op"], op["key"]
    if kind == "put":
        work["entries"][key] = op["value"]
        obs = f"put:{key}"
    elif kind == "delete":
        if key not in work["entries"]:
            return False, "missing_key"
        del work["entries"][key]
        obs = f"delete:{key}"
    elif kind == "allocate":
        value = f"id-{work['next_identifier']}"
        work["next_identifier"] += 1          # never reused, even if the entry is later deleted
        work["entries"][key] = value
        obs = f"allocate:{key}={value}"
    else:  # draw
        value = draw_value(seed, work["random_cursor"])
        work["random_cursor"] += 1
        work["entries"][key] = value
        obs = f"draw:{key}={value}"
    work["logical_tick"] += 1
    return True, obs


def apply_operation(state: StoreState, op: dict, seed: str) -> Transition:
    validate_operation(op)
    work = {"entries": dict(state.entries), "next_identifier": state.next_identifier,
            "logical_tick": state.logical_tick, "random_cursor": state.random_cursor}
    members = op["ops"] if op["op"] == "txn" else [op]
    if op["op"] == "txn" and not members:
        return Transition(state, (), False, "empty_transaction")
    observations = []
    for m in members:
        ok, info = _step(work, m, seed)
        if not ok:                       # all-or-nothing: temp state is simply discarded, counters untouched
            return Transition(state, (), False, info)
        observations.append(info)
    new = StoreState(tuple(sorted(work["entries"].items())), work["next_identifier"], work["logical_tick"],
                     work["random_cursor"], state.operation_cursor + 1)
    return Transition(new, tuple(observations), True)


def run(ops: list[dict], seed: str, state: StoreState = StoreState()) -> list[Transition]:
    out = []
    for op in ops:
        t = apply_operation(state, op, seed)
        out.append(t)
        state = t.state
    return out
