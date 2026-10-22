"""Graph reducers: shrink a dependency graph to a valid cycle witness without breaking reference integrity."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator

from ..canonical import canonical_bytes, sha256_digest
from ..graph import find_cycle, stable_topological_order
from ..minimize import FailureSignature, OracleOutcome


@dataclass(frozen=True)
class GraphCase:
    nodes: tuple[str, ...]
    edges: tuple[tuple[str, str], ...]

    def identity(self) -> str:
        return sha256_digest(canonical_bytes({"nodes": sorted(self.nodes), "edges": sorted(map(list, self.edges))}))

    def cost(self) -> tuple:
        return (len(self.edges), len(self.nodes), canonical_bytes({"n": sorted(self.nodes), "e": sorted(map(list, self.edges))}))


class CycleOracle:
    identity = "graph.cycle/1"
    SIGNATURE = FailureSignature("graph.acyclic", "1", "graph.acyclic", "cycle_present", "cycle")

    def evaluate(self, case: GraphCase) -> OracleOutcome:
        nodes = set(case.nodes)
        if len(nodes) != len(case.nodes) or any(a not in nodes or b not in nodes for a, b in case.edges):
            return OracleOutcome.INVALID_CANDIDATE       # dangling reference or duplicate node
        _, residual = stable_topological_order(nodes, case.edges)
        cycle, _ = find_cycle(residual, case.edges) if residual else (None, False)
        return OracleOutcome.SAME_FAILURE if cycle else OracleOutcome.NO_FAILURE

    def signature_of(self, case: GraphCase):
        return self.SIGNATURE if self.evaluate(case) == OracleOutcome.SAME_FAILURE else None


class DeleteEdges:
    name = "delete_edges"
    description = "deletion of single edges (nodes kept)"

    def proposals(self, case: GraphCase) -> Iterator[tuple[str, GraphCase]]:
        for i, e in enumerate(case.edges):
            yield f"delete_edge[{e[0]}->{e[1]}]", GraphCase(case.nodes, tuple(x for j, x in enumerate(case.edges) if j != i))


class DeleteNodesWithClosure:
    """Distinct operator from edge deletion: removing a node also removes its incident edges (dependent closure)."""
    name = "delete_nodes_with_closure"
    description = "deletion of a node together with its incident edges"

    def proposals(self, case: GraphCase) -> Iterator[tuple[str, GraphCase]]:
        for n in sorted(case.nodes):
            yield (f"delete_node_closure[{n}]",
                   GraphCase(tuple(x for x in case.nodes if x != n), tuple(e for e in case.edges if n not in e)))


class DeleteNodesRejectingDependents:
    """Deletes a node only when no edge references it, otherwise the candidate is simply not proposed."""
    name = "delete_unreferenced_nodes"
    description = "deletion of nodes that no edge references"

    def proposals(self, case: GraphCase) -> Iterator[tuple[str, GraphCase]]:
        used = {x for e in case.edges for x in e}
        for n in sorted(case.nodes):
            if n not in used:
                yield f"delete_unreferenced[{n}]", GraphCase(tuple(x for x in case.nodes if x != n), case.edges)
