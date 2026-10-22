"""Activity dependency graph (chapter 03): stable ordering, cycle witnesses, closure, invalidation paths.

Ordering and closure are separate checks: an acyclic graph with a missing input is still incomplete.
"""
from __future__ import annotations

import heapq
from dataclasses import dataclass

from .model import Bundle

MAX_WITNESS = 64


@dataclass(frozen=True)
class DependencyResolution:
    consumer_id: str
    required_ref: str
    kind: str                 # producer | external | missing
    resolved_ref: str | None


@dataclass(frozen=True)
class GraphInspection:
    order: tuple[str, ...]                  # stable topological order of the acyclic part
    cycle: tuple[str, ...] | None           # repeated-node path; first == last; may be truncated
    cycle_truncated: bool
    residual: tuple[str, ...]               # nodes never emitted (cyclic or downstream of a cycle)
    resolutions: tuple[DependencyResolution, ...]
    problems: tuple[dict, ...]              # duplicate edges, self deps, collisions, conflicting declarations

    @property
    def acyclic(self) -> bool:
        return self.cycle is None

    @property
    def closed(self) -> bool:
        return not any(r.kind == "missing" for r in self.resolutions)

    def to_dict(self) -> dict:
        return {"order": list(self.order), "acyclic": self.acyclic,
                "cycle": None if self.cycle is None else list(self.cycle),
                "cycle_truncated": self.cycle_truncated, "residual": list(self.residual),
                "closed": self.closed,
                "missing": [{"consumer": r.consumer_id, "required": r.required_ref}
                            for r in self.resolutions if r.kind == "missing"],
                "problems": list(self.problems)}


def stable_topological_order(nodes, edges) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Kahn with a min-heap: reproducible regardless of input order. Returns (order, residual)."""
    nodes = sorted(set(nodes))
    adj: dict[str, list[str]] = {n: [] for n in nodes}
    indeg = {n: 0 for n in nodes}
    for a, b in sorted(set(edges)):
        adj[a].append(b)
        indeg[b] += 1
    ready = [n for n in nodes if indeg[n] == 0]
    heapq.heapify(ready)
    order = []
    while ready:
        n = heapq.heappop(ready)
        order.append(n)
        for m in adj[n]:
            indeg[m] -= 1
            if indeg[m] == 0:
                heapq.heappush(ready, m)
    done = set(order)
    return tuple(order), tuple(n for n in nodes if n not in done)


def find_cycle(residual, edges, limit: int = MAX_WITNESS) -> tuple[tuple[str, ...] | None, bool]:
    """Iterative DFS over the residual subgraph; returns an actual cycle, not every downstream node."""
    res = set(residual)
    adj: dict[str, list[str]] = {n: [] for n in res}
    for a, b in sorted(set(edges)):
        if a in res and b in res:
            adj[a].append(b)
    color: dict[str, int] = {}
    for start in sorted(res):
        if color.get(start):
            continue
        stack = [(start, iter(adj[start]))]
        path = [start]
        color[start] = 1
        while stack:
            node, it = stack[-1]
            nxt = next(it, None)
            if nxt is None:
                color[node] = 2
                stack.pop()
                path.pop()
            elif color.get(nxt) == 1:
                cyc = path[path.index(nxt):] + [nxt]
                return (tuple(cyc[:limit + 1]), len(cyc) > limit + 1)
            elif not color.get(nxt):
                color[nxt] = 1
                stack.append((nxt, iter(adj[nxt])))
                path.append(nxt)
    return None, False


def inspect_bundle(bundle: Bundle, external_assumptions: frozenset[str] = frozenset()) -> GraphInspection:
    """Build the graph from activity.depends_on plus artifact producer/consumer links."""
    problems: list[dict] = []
    ids = {a.id for a in bundle.activities}
    producers: dict[str, list[str]] = {}
    for act in bundle.activities:
        for out in act.outputs:
            producers.setdefault(out, []).append(act.id)
    for art, who in sorted(producers.items()):
        if len(who) > 1:
            problems.append({"code": "producer_collision", "artifact": art, "activities": sorted(who)})
    edges: list[tuple[str, str]] = []
    resolutions: list[DependencyResolution] = []
    seen_deps: set[tuple[str, str]] = set()
    for act in sorted(bundle.activities, key=lambda a: a.id):
        for dep in act.depends_on:
            if dep == act.id:
                problems.append({"code": "self_dependency", "activity": act.id})
            elif dep not in ids:
                resolutions.append(DependencyResolution(act.id, dep, "missing", None))
            else:
                if (dep, act.id) in seen_deps:
                    problems.append({"code": "duplicate_edge", "from": dep, "to": act.id})
                seen_deps.add((dep, act.id))
                edges.append((dep, act.id))
        for inp in act.inputs:
            who = sorted(producers.get(inp, []))
            if who and inp in external_assumptions:
                problems.append({"code": "conflicting_declaration", "artifact": inp})
            if who:
                resolutions.append(DependencyResolution(act.id, inp, "producer", who[0]))
                if who[0] != act.id and who[0] in ids:
                    edges.append((who[0], act.id))
            elif inp in external_assumptions:
                resolutions.append(DependencyResolution(act.id, inp, "external", inp))
            else:
                # a source artifact with no producer is an *undeclared* external input, not an inferred exemption
                resolutions.append(DependencyResolution(act.id, inp, "missing", None))
    order, residual = stable_topological_order(ids, edges)
    cycle, trunc = find_cycle(residual, edges) if residual else (None, False)
    return GraphInspection(order, cycle, trunc, residual, tuple(resolutions), tuple(problems))


def reverse_dependents(edges) -> dict[str, list[str]]:
    rev: dict[str, list[str]] = {}
    for a, b in sorted(set(edges)):
        rev.setdefault(a, []).append(b)
    return rev


def invalidation_paths(edges, changed: str) -> dict[str, tuple[str, ...]]:
    """Every node reachable from `changed`, with one witness path each (BFS, sorted: deterministic).
    Nodes without a path to the change are never returned, so unrelated branches stay valid."""
    rev = reverse_dependents(edges)
    paths: dict[str, tuple[str, ...]] = {}
    frontier = [(changed, (changed,))]
    seen = {changed}
    while frontier:
        nxt = []
        for node, path in frontier:
            for m in rev.get(node, []):
                if m not in seen:
                    seen.add(m)
                    paths[m] = path + (m,)
                    nxt.append((m, path + (m,)))
        frontier = nxt
    return paths
