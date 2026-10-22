"""Sequence reducers for ExperimentCase: complete-unit deletion (ddmin chunks) and transaction-member deletion."""
from __future__ import annotations

from typing import Iterator

from ..experiments.checkpoint import ExperimentCase


def _delete_units(case: ExperimentCase, drop: set[int]) -> ExperimentCase:
    ops = tuple(op for i, op in enumerate(case.ops) if i not in drop)
    boundary = case.boundary - sum(1 for i in drop if i < case.boundary)   # the checkpoint stays between the same units
    return ExperimentCase(case.seed, ops, boundary, case.defects)


class DeleteUnits:
    """Default sequence reducer: deletes whole top-level units (a transaction is never split)."""
    name = "delete_units"
    description = "deletion of contiguous chunks and single top-level operations/transactions"

    def proposals(self, case: ExperimentCase) -> Iterator[tuple[str, ExperimentCase]]:
        n = len(case.ops)
        g = 2
        while n:
            size = max(1, n // g)
            for start in range(0, n, size):
                chunk = set(range(start, min(n, start + size)))
                if len(chunk) < n or n == 1:
                    yield f"delete_chunk[{start}:{start + len(chunk)}]/g{g}", _delete_units(case, chunk)
            if size == 1:
                break
            g = min(n, g * 2)


class DeleteTransactionMembers:
    """Dedicated transaction reducer: removes single members while keeping the transaction (atomicity) intact."""
    name = "delete_txn_members"
    description = "deletion of single members inside a transaction (transaction kept)"

    def proposals(self, case: ExperimentCase) -> Iterator[tuple[str, ExperimentCase]]:
        for i, op in enumerate(case.ops):
            if op["op"] != "txn":
                continue
            for j in range(len(op["ops"])):
                members = [m for k, m in enumerate(op["ops"]) if k != j]
                if members:
                    ops = list(case.ops)
                    ops[i] = dict(op, ops=members)
                    yield f"delete_member[{i}.{j}]", ExperimentCase(case.seed, tuple(ops), case.boundary, case.defects)
