# 03 Composition and dependency solver

## Goal

Implement a solver that distinguishes valid individual components from a justified composed result. A file can be intact and an operation locally correct while their composition is invalid because units, generations, representations, or assumptions disagree. The solver establishes only obligations defined by registered rules; it does not infer arbitrary program semantics from names.

Create `graph.py`, `obligations.py`, and `rules/composition.py`. Represent an obligation as a claim about an activity input, an activity output, or an edge between them. Rules describe preconditions and postconditions using a deliberately small declarative vocabulary. Version one supports exact tag equality, required membership, generation equality, and explicit prerequisite references. Rich logical languages can be added later without pretending the first version is a theorem prover.

## Graph construction

Index activities by ID and artifacts by ID. A dependency edge points from prerequisite activity to consumer activity. Validate duplicate edges, self-dependencies, missing activity IDs, and conflicting artifact producers. A missing producer is not automatically invalid: an artifact may be a declared external input. It must have an explicit assumption or verified local source, not an inferred exemption.

```python
@dataclass(frozen=True)
class DependencyResolution:
    consumer_id: str
    required_ref: str
    kind: str  # producer, external_assumption, missing
    resolved_ref: str | None

def resolve_dependencies(bundle: ValidatedBundle) -> GraphInspection: ...
def stable_topological_order(graph: ActivityGraph) -> tuple[str, ...]: ...
def check_composition(context: CheckContext, claim: Claim) -> Finding: ...
```

Do not treat a topological ordering as evidence that all dependencies exist. Ordering and closure are separate checks. An acyclic graph with a missing input is still incomplete.

## Stable topological ordering and cycles

Use Kahn's algorithm with a min-heap of ready activity IDs. Build sorted adjacency lists, count indegrees, repeatedly pop the smallest ready ID, and decrement dependents. Complexity is approximately `O((V + E) log V)` with heap ordering. This produces reproducible order independent of input insertion.

If nodes remain, find a cycle in the residual graph using iterative depth-first search with sorted neighbors and an explicit stack. Return the repeated-node path as a witness. A residual node can be downstream from a cycle without belonging to it, so do not report every residual node as cyclic. Enforce graph-size and witness-size limits; truncation must be explicit. A bounded witness is not necessarily the shortest cycle. A later reduction pass may shorten it.

## Composition obligations

For each consumer input, establish artifact identity and provenance, then compare the producer guarantee to the consumer requirement. Define guarantee facts in registered rule output, not in unrestricted producer assertions. A producer may claim a tag, but a verifying rule must establish it or the composed result remains conditional on an assumption.

Example: a fictional text processor outputs normalized text and character offsets. A consumer requires original-byte offsets. Both operations can pass local tests, but the offset-representation edge fails. The witness contains the producer activity, consumer activity, requirement, established guarantee, and the relevant artifact references.

Example: one operation produces a quantity in seconds and another interprets an unqualified integer as milliseconds. The solver must reject the edge unless an explicit verified conversion activity connects them. It must not infer a conversion merely from a familiar unit name.

## Verdict propagation

Do not reduce all outcomes using a numeric severity maximum. Evaluate each declared obligation and retain its finding. The composed claim is contradicted if a valid witness shows an essential obligation is false. It is supported only when every essential obligation is supported under the accumulated assumptions. Missing essential evidence produces insufficient evidence. Invalid evidence prevents using that evidence; other independent contradictions may still be reported separately.

Human review required is reserved for a profile that explicitly delegates a semantic decision to a person. It is not a synonym for an exception or uncertainty. Not applicable requires an established applicability condition, not absence of information. Assumptions propagate by union and remain visible in the final finding. Adding assumptions cannot make a conditional claim look unconditional.

## Incremental invalidation

Maintain reverse dependencies after a full validated graph build. When an input changes, mark direct consumers dirty and traverse their dependents. Changes to unrelated components should not invalidate a claim with no path to them. Every invalidation includes a path from changed evidence to affected claim.

The cache key includes the claim, transitive evidence identities, selected rule versions, profile identity, and assumption identities. A cached supported result cannot survive a changed comparator or generation simply because the raw artifact bytes stayed equal. Start with whole-claim invalidation; optimize subgraph caching only after conformance tests exist.

## Edge cases

Handle diamond dependencies without duplicate evaluation. Reject contradictory declarations that classify the same required input as both an internal product and an assumed external source. A branch not required by the selected claim can remain unresolved without blocking that claim, but the report must not describe the entire graph as verified. Inputs with identical bytes but different semantic units remain distinct evidence records.

Cycles may be legitimate in executable systems with explicit iteration. Version one composition graphs remain acyclic; model an iteration as an opaque activity with a separate bounded invariant rule. Do not silently cut a cycle to obtain an order.

## Tests and acceptance

Unit tests cover chains, diamonds, disconnected branches, self-cycles, multi-node cycles, missing references, declared assumptions, producer collisions, and shuffled insertion. Composition tests deliberately create locally valid but globally incompatible pairs. Invalidation tests change an ancestor, a descendant, and an unrelated branch, checking exact affected sets and witness paths.

Acceptance requires identical ordering and witnesses for equivalent input permutations; explicit missing-input diagnostics; no unconditional support from assumed external facts; and a synthetic end-to-end failure where local checks pass but a composition rule correctly contradicts the final claim. Store that example as a public fixture written specifically for this project.
