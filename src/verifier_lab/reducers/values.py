"""Value simplifiers for ExperimentCase, in a fixed order, each with a strictly decreasing cost."""
from __future__ import annotations

from typing import Iterator

from ..experiments.checkpoint import ExperimentCase


def _walk(op):
    yield op
    for m in op.get("ops", ()):
        yield m


class SimplifyValues:
    name = "simplify_values"
    description = "put values toward the empty string, keys renamed to k0,k1,.. (a semantics-preserving rename), seed toward '0'"

    def proposals(self, case: ExperimentCase) -> Iterator[tuple[str, ExperimentCase]]:
        # 1. shorten put values, one operation at a time
        for i, op in enumerate(case.ops):
            members = op["ops"] if op["op"] == "txn" else None
            if members is None:
                if op["op"] == "put" and op["value"] != "":
                    ops = list(case.ops)
                    ops[i] = dict(op, value="")
                    yield f"empty_value[{i}]", ExperimentCase(case.seed, tuple(ops), case.boundary, case.defects)
            else:
                for j, m in enumerate(members):
                    if m["op"] == "put" and m["value"] != "":
                        new = list(members)
                        new[j] = dict(m, value="")
                        ops = list(case.ops)
                        ops[i] = dict(op, ops=new)
                        yield f"empty_value[{i}.{j}]", ExperimentCase(case.seed, tuple(ops), case.boundary, case.defects)
        # 2. canonical key renaming (one global, injective rename: references stay consistent)
        order: list[str] = []
        for op in case.ops:
            for m in _walk(op):
                if "key" in m and m["key"] not in order:
                    order.append(m["key"])
        mapping = {k: f"k{n}" for n, k in enumerate(order)}
        if any(k != v for k, v in mapping.items()) and len(set(mapping.values())) == len(mapping):
            def ren(op):
                out = dict(op)
                if "key" in out:
                    out["key"] = mapping[out["key"]]
                if "ops" in out:
                    out["ops"] = [ren(m) for m in out["ops"]]
                return out
            yield "rename_keys", ExperimentCase(case.seed, tuple(ren(o) for o in case.ops), case.boundary, case.defects)
        if case.seed != "0":
            yield "simplify_seed", ExperimentCase("0", case.ops, case.boundary, case.defects)
