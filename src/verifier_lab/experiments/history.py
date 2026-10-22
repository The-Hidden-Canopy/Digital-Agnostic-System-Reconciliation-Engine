"""Deterministic operation-history generator (seeded SHA-256 counter stream; chapter 02)."""
from __future__ import annotations

from ..detstream import DetStream

KINDS = ("put", "delete", "draw", "allocate", "txn")


def generate_history(seed: str, n: int, keys: int = 10) -> list[dict]:
    ds = DetStream("history/" + seed)

    def simple(kind=None) -> dict:
        kind = kind or ("put", "delete", "draw", "allocate")[ds.randbelow(4)]
        op = {"op": kind, "key": f"key{ds.randbelow(keys)}"}
        if kind == "put":
            op["value"] = "v" * (1 + ds.randbelow(8))
        return op

    out = []
    for _ in range(n):
        if ds.randbelow(8) == 0:
            out.append({"op": "txn", "ops": [simple() for _ in range(1 + ds.randbelow(3))]})
        else:
            out.append(simple())
    return out
