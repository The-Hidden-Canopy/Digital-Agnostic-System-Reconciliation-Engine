# 07 Failure minimization

## Goal

Reduce a witnessed failure into a smaller experiment without changing what failed. Implement `minimize.py`, `reducers/sequence.py`, `reducers/graph.py`, and `reducers/values.py`. The reducer is a search client of the experiment runner and independent checker, not a second implementation of the invariant.

The output is a candidate reproducer, its validation evidence, and a precise minimality statement. Do not promise the globally smallest counterexample. A practical reducer can establish that no permitted single deletion preserves the failure, or report the best candidate found within a recorded budget.

## Failure identity

```python
@dataclass(frozen=True)
class FailureSignature:
    rule_id: str
    rule_version: str
    claim_predicate: str
    reason_code: str
    witness_class: str

class OracleOutcome(StrEnum):
    SAME_FAILURE = 'same_failure'
    NO_FAILURE = 'no_failure'
    INVALID_CANDIDATE = 'invalid_candidate'
    DIFFERENT_FAILURE = 'different_failure'
    UNRESOLVED = 'unresolved'

def minimize(case: ExperimentCase, signature: FailureSignature,
             oracle: FailureOracle, budget: ReductionBudget) -> ReductionResult: ...
```

Capture the original finding before shrinking. A parser crash after removing required setup is not the same failure as an allocator-continuity defect. Match a stable signature and, where necessary, a semantic witness predicate. Do not include exact operation positions in the signature when those positions should change as irrelevant operations are deleted.

## Candidate validity

Every proposed reduction passes structural and experiment-precondition validation before execution. References must resolve, transaction boundaries remain valid, and required setup remains present. A reducer may rewrite references only through a specified semantics-preserving transformation. It must not silently invent a different starting state to force a failure.

Dependency-aware deletion removes an operation and either rejects the candidate if consumers require it or removes the dependent closure. Keep those two operators distinct in the reduction log. Removing a transaction member can change atomic semantics, so the default sequence reducer deletes complete transactions. A dedicated transaction reducer can simplify members while preserving the declared failure class.

## Deterministic reduction algorithm

Start with chunk deletion similar to delta debugging. Split the current sequence into a deterministic number of contiguous chunks. Try deleting each chunk in order. Accept the first candidate that reproduces the same failure, restart from the accepted smaller case, and increase granularity when no chunk can be removed. After coarse deletion, run a single-operation fixed-point pass.

Next prune unused graph branches and simplify values. Candidate order is fixed: smaller integers toward zero, shorter strings toward empty where valid, fewer map entries, shorter operation payloads, and simpler timing gaps. Each simplifier has a well-founded size measure so accepted transformations cannot oscillate. Define a lexicographic cost such as operation count, graph-edge count, total payload bytes, and canonical byte order.

```text
best = validate_and_reproduce(original)
for reducer in ordered_reducers:
    repeat:
        accepted = false
        for candidate in reducer.proposals(best):
            if budget exhausted: return best with incomplete scope
            if valid(candidate) and oracle(candidate) == SAME_FAILURE:
                best = candidate
                accepted = true
                break
        until not accepted
reproduce best in a fresh process
return best with tested minimality scope
```

A complete fixed point for one reducer can be disturbed by a later simplifier. Run the ordered reducer sequence again until no accepted change occurs, or report the configured iteration limit. The final minimality label names which operators were exhausted against the final candidate.

## Oracle caching and reproducibility

Cache oracle results by canonical candidate identity, subject implementation identity, rule version, environment profile, and relevant execution configuration. A cached result from a different executable is not valid. Keep operational errors distinct; do not cache a transient timeout as a permanent semantic verdict unless the experiment explicitly tests bounded execution time under controlled conditions.

Reproduce the original failure before reduction. If it is unstable, record repeated outcomes and stop deterministic minimality claims. A separate stochastic reducer may require a configured reproduction rate, but its result is a probabilistic observation and must include sample counts and conditions. The initial implementation should return unresolved for flaky failures rather than hide the instability.

Deterministic budgets count oracle invocations and accepted reductions. A protective wall-clock deadline can interrupt the run, but then the output is a best-so-far result. Persist progress only to a new result generation so a crash does not replace a known reproducer with an unverified candidate.

## Counterexample package

Store the original case identity, reduced case, failure signature, pinned runner/checker identities, final finding, reduction log, and search scope. The log records candidate identity, operator, oracle outcome, and acceptance decision. Large intermediate payloads may be omitted if every retained accepted step can be reconstructed from the original and logged transformation.

Verify the final reproducer in a fresh process without reducer state. The package verifier checks integrity and re-evaluates the witness. It must not trust the reducer's assertion that a case failed. If independent reproduction disagrees, preserve both results and mark the reduction unverified.

## Acceptance tests

Use a synthetic long history containing one relevant allocate-delete-checkpoint-restore sequence and many irrelevant writes. The reducer should remove irrelevant operations while preserving the identity-continuity failure. A setup-deletion variant must return invalid candidate or different failure, never count as success. A cyclic dependency graph must shrink to a valid cycle witness without breaking reference integrity.

Test empty and one-operation cases, atomic groups, shared dependencies, equal-cost candidates, budget exhaustion, cached duplicates, and unstable oracles. Include a deliberately wrong reducer that accepts any exception; the conformance suite must reject its output because the failure signature changed.

Acceptance requires a fresh-process reproduction and a truthful scope such as “1-minimal under single-transaction deletion and the listed value simplifiers.” A short counterexample is useful only if it still proves the intended defect and can be checked independently.
