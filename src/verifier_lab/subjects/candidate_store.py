"""Candidate store under test: mutable and written independently of the reference (shares no transition code).

`defects` names deliberately injected bugs used as laboratory positive controls (chapter 05).
"""
from __future__ import annotations

import hashlib

DEFECTS = ("omit_random_cursor", "reset_next_id", "double_cursor_after_restore", "flatten_txn", "sort_replay")


class CandidateStore:
    def __init__(self, seed: str, defects: frozenset[str] = frozenset()):
        unknown = set(defects) - set(DEFECTS)
        if unknown:
            raise ValueError(f"unknown defects {sorted(unknown)}")
        self.seed, self.defects = seed, frozenset(defects)
        self.kv: dict[str, str] = {}
        self.next_id = 0
        self.tick = 0
        self.rng_pos = 0
        self.cursor = 0
        self._bump_extra = False

    # -- state boundary: the checker only sees these decoded dicts
    def snapshot(self) -> dict:
        return {"entries": [[k, self.kv[k]] for k in sorted(self.kv)], "next_identifier": self.next_id,
                "logical_tick": self.tick, "random_cursor": self.rng_pos, "operation_cursor": self.cursor}

    def load(self, d: dict) -> None:
        self.kv = {k: v for k, v in d["entries"]}
        self.tick, self.cursor = d["logical_tick"], d["operation_cursor"]
        self.next_id = d["next_identifier"]
        self.rng_pos = d["random_cursor"]
        if "omit_random_cursor" in self.defects:
            self.rng_pos = 0
        if "reset_next_id" in self.defects:       # infers the counter from visible entries: deleted ids are forgotten
            ids = [int(v[3:]) for v in self.kv.values() if v.startswith("id-") and v[3:].isdigit()]
            self.next_id = max(ids) + 1 if ids else 0
        self._bump_extra = "double_cursor_after_restore" in self.defects

    # -- operations
    def _one(self, op, kv, counters) -> tuple[bool, str]:
        t, key = op["op"], op["key"]
        if t == "put":
            kv[key] = op["value"]
            note = f"put:{key}"
        elif t == "delete":
            if key not in kv:
                return False, "missing_key"
            kv.pop(key)
            note = f"delete:{key}"
        elif t == "allocate":
            v = "id-%d" % counters[0]
            counters[0] += 1
            kv[key] = v
            note = f"allocate:{key}={v}"
        else:
            v = hashlib.sha256(("%s|%d" % (self.seed, counters[1])).encode()).hexdigest()[:8]
            counters[1] += 1
            kv[key] = v
            note = f"draw:{key}={v}"
        counters[2] += 1
        return True, note

    def apply(self, op: dict) -> tuple[bool, tuple[str, ...]]:
        if op["op"] != "txn":
            members = [op]
        else:
            members = op["ops"]
            if not members:
                return False, ()
        if op["op"] == "txn" and "flatten_txn" in self.defects:
            notes = []
            counters = [self.next_id, self.rng_pos, self.tick]
            for m in members:                    # defect: members committed one by one, no atomicity
                ok, note = self._one(m, self.kv, counters)
                self.next_id, self.rng_pos, self.tick = counters
                if not ok:
                    return False, ()
                notes.append(note)
            self.cursor += 1
            return True, tuple(notes)
        kv, counters, notes = dict(self.kv), [self.next_id, self.rng_pos, self.tick], []
        for m in members:
            ok, note = self._one(m, kv, counters)
            if not ok:
                return False, ()
            notes.append(note)
        self.kv, (self.next_id, self.rng_pos, self.tick) = kv, counters
        self.cursor += 2 if self._bump_extra else 1
        self._bump_extra = False
        return True, tuple(notes)

    def replay(self, ops: list[dict]):
        """Replay entry point used by the third path. `sort_replay` reorders noncommutative operations."""
        seq = sorted(ops, key=lambda o: o.get("key", "")) if "sort_replay" in self.defects else ops
        for op in seq:
            yield (op, *self.apply(op))
