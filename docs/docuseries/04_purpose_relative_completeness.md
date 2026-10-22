# 04 Purpose relative completeness

## Goal

A package is not simply complete or incomplete. It may be sufficient to inspect a result, insufficient to reproduce that result, and insufficient to resume the computation exactly. Implement explicit purpose profiles that state which evidence is required for a particular claim. This separates capability from presentation and prevents a weakly verified artifact from being promoted into a stronger assertion.

Create `purposes.py` and `rules/completeness.py`. Profiles are declarative local documents with stable IDs, versions, and digests. They contain required evidence roles, optional roles, applicability conditions, coherence constraints, and rule references. Profiles must not contain executable Python, SQL, shell commands, or dynamic import paths. Selecting a profile never grants permission to execute code.

## Profile model

```python
@dataclass(frozen=True)
class Requirement:
    role: str
    minimum_count: int
    maximum_count: int | None
    predicate: str

@dataclass(frozen=True)
class PurposeProfile:
    profile_id: str
    profile_version: str
    requirements: tuple[Requirement, ...]
    optional_roles: tuple[str, ...]
    coherence_rules: tuple[str, ...]

def assess_completeness(
    context: CheckContext, claim: Claim, profile: PurposeProfile
) -> tuple[Finding, ...]: ...
```

The profile's role mapping is supplied in the selected claim's arguments as references, not inferred from filenames. A file called `checkpoint.json` does not establish that it is a checkpoint. Cardinality bounds are explicit profile parameters and must be validated. A minimum greater than its maximum is an invalid profile, not evidence that the subject failed.

## A fictional example

For the synthetic key-value process, define an `inspect_values` purpose requiring a map artifact and a decoding rule. Define `continue_exactly` requiring that map, an operation cursor, a random-source state, an allocator counter, and a pinned transition semantics identity. Both are fresh public fixture profiles.

A package containing only a valid map supports inspection while yielding insufficient evidence for exact continuation. If it supplies an allocator field of the wrong type, that supplied record is invalid evidence. If all fields are valid but the restored continuation produces a different next identifier, an executed experiment can contradict the exact-continuation claim.

Do not use the absence of an optional role to invent a value. Conversely, optional does not mean unchecked: malformed optional data must be reported as invalid, and any claim depending on it must not pass. A profile can explicitly declare unrelated supplemental artifacts outside its evaluation scope; report that scope rather than treating the entire package as verified.

## Coherent generation algorithm

Completeness checks answer whether required roles are present. Coherence checks answer whether those roles belong together. Resolve all role references, validate each artifact, and obtain generation facts from independently checked activity records or a declared assumption. Compare the identities required by the profile. A group of individually valid artifacts from different generations is not a valid checkpoint group.

```text
resolve profile and validate profile itself
resolve role references without guessing by path
for each required role in stable order:
    check cardinality and availability
    evaluate the role's decoding or integrity obligation
for every supplied optional role:
    validate it if it is within the profile's scope
evaluate generation and prerequisite constraints
return all findings plus a scoped summary
```

A role cannot satisfy two incompatible requirements just because the same ID is reused. Profiles may allow shared artifacts explicitly, but distinct expected semantic types still need their own checking obligations. References to an old operation cursor and new state map must fail coherence even when both files have correct hashes.

## Lifecycle prerequisites

Use a generic directed prerequisite graph for lifecycle assertions. For example, a profile may require observation before claiming persistence, and verified persistence before claiming recoverability. These are explicit profile rules, not universal meanings inferred from English words. Require evidence of each prerequisite or report insufficiency. A producer's Boolean field saying “persisted” is an assertion, not proof that bytes survived a restart.

Lifecycle graphs must be acyclic in version one. A reversal such as invalidating a previous result is a new record tied to the affected claim, not mutation of history. When determining current validity, consider the most recent applicable status in a verified ordering. If ordering is ambiguous, do not select a preferred status. Return insufficient evidence for the current-validity claim and retain the competing records.

## Profile strengthening

A stronger profile can add requirements but must not erase the assumptions of a weaker result. Define strengthening only when the requirement relation is mechanically established; do not compare profiles by their names. Test monotonic behavior: removing required evidence cannot turn an unsupported strong-purpose claim into support. Adding valid evidence can resolve insufficiency, but adding contradictory evidence may defeat a previously supported claim.

Versioned profiles are immutable. If an organization wants a different policy, it creates a different profile identity. Historical findings keep their original profile and are not silently reevaluated under a newer one. The UI should show when a selected profile differs from the one used to produce an old report and offer an explicit new evaluation.

## Failure classification and resource bounds

An unavailable artifact yields insufficiency for its requirement. A digest mismatch or malformed supplied role yields invalid evidence. A valid cross-generation witness contradicts coherence. An unmet human interpretation requirement yields human review required only when the profile explicitly calls for that decision. A profile not relevant to the claim yields not applicable only after its applicability condition is established.

A profile recursion limit, too many requirements, or a disabled rule are operational or configuration problems. Do not mislabel them as failures of the subject. Record unexecuted obligations and avoid emitting an overall supported summary while any essential obligation remains unexecuted.

## Acceptance tests

Create a matrix of weak and strong purposes against complete, incomplete, malformed, mixed-generation, invalidated, and ambiguously ordered packages. Assert exact per-obligation reason codes. Test role aliases, cardinality conflicts, unknown profile versions, changed profile digests, and duplicate lifecycle entries.

Acceptance requires a single package to receive different justified outcomes for two purposes, a torn-generation package to fail despite valid individual hashes, and an older success never to override a later verified invalidation. The report must identify what additional evidence would resolve an insufficiency without promising that supplying it will necessarily establish the claim.
