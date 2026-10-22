#!/usr/bin/env python3
"""Example subject program for `verifier-lab experiment checkpoint --subject-cmd`.

A deliberately small, standalone key-value store speaking the JSON-lines protocol (see verifier_lab/subjects/external.py).
Set SUBJECT_DEFECTS=reset_next_id,omit_random_cursor (comma separated) to give it a bug to find.
Only the Python standard library is used; copy this file as the starting point for your own subject.
"""
import hashlib
import json
import os
import sys

DEFECTS = set(filter(None, os.environ.get("SUBJECT_DEFECTS", "").split(",")))


class Store:
    def __init__(self, seed):
        self.seed, self.kv = seed, {}
        self.next_id = self.tick = self.rng = self.cursor = 0

    def one(self, op, kv, c):                        # c = [next_id, rng, tick]
        t, k = op["op"], op["key"]
        if t == "put":
            kv[k] = op["value"]; note = f"put:{k}"
        elif t == "delete":
            if k not in kv:
                return False, None
            del kv[k]; note = f"delete:{k}"
        elif t == "allocate":
            v = f"id-{c[0]}"; c[0] += 1; kv[k] = v; note = f"allocate:{k}={v}"
        elif t == "draw":
            v = hashlib.sha256(f"{self.seed}|{c[1]}".encode()).hexdigest()[:8]; c[1] += 1; kv[k] = v; note = f"draw:{k}={v}"
        else:
            raise ValueError(f"unknown op {t}")
        c[2] += 1
        return True, note

    def apply(self, op):
        members = op["ops"] if op["op"] == "txn" else [op]
        if not members:
            return False, []
        kv, c, notes = dict(self.kv), [self.next_id, self.rng, self.tick], []
        for m in members:
            ok, note = self.one(m, kv, c)
            if not ok:
                return False, []
            notes.append(note)
        self.kv, (self.next_id, self.rng, self.tick) = kv, c
        self.cursor += 1
        return True, notes

    def snapshot(self):
        return {"entries": [[k, self.kv[k]] for k in sorted(self.kv)], "next_identifier": self.next_id,
                "logical_tick": self.tick, "random_cursor": self.rng, "operation_cursor": self.cursor}

    def load(self, s):
        self.kv = {k: v for k, v in s["entries"]}
        self.tick, self.cursor, self.next_id, self.rng = s["logical_tick"], s["operation_cursor"], s["next_identifier"], s["random_cursor"]
        if "omit_random_cursor" in DEFECTS:
            self.rng = 0
        if "reset_next_id" in DEFECTS:
            ids = [int(v[3:]) for v in self.kv.values() if v.startswith("id-") and v[3:].isdigit()]
            self.next_id = max(ids) + 1 if ids else 0


def main():
    store = None
    for line in sys.stdin:
        req = json.loads(line)
        cmd = req["cmd"]
        try:
            if cmd == "init":
                store = Store(req["seed"]); resp = {"ok": True}
            elif cmd == "apply":
                ok, notes = store.apply(req["op"]); resp = {"ok": True, "accepted": ok, "observations": notes}
            elif cmd == "snapshot":
                resp = {"ok": True, "state": store.snapshot()}
            elif cmd == "load":
                store.load(req["state"]); resp = {"ok": True}
            else:
                resp = {"ok": False, "error": "unsupported"}        # e.g. "replay": the adapter falls back to sequential apply
        except Exception as exc:
            resp = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        sys.stdout.write(json.dumps(resp) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
